"""Behavioral checks of generic settings, model contracts and all three adapters."""
import asyncio
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from mdfoam_droplet_viewer import server
from mdfoam_droplet_viewer.demo import generate
from mdfoam_droplet_viewer.droplet import contact_for_settings
from mdfoam_droplet_viewer.models import ModelRegistry
from mdfoam_droplet_viewer.service import operate
from mdfoam_droplet_viewer.settings import Settings, water_settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE_ROOT", tmp_path/'cache')
    monkeypatch.setattr(server, "S", server.State())
    server.S.analyzer.workers = 1
    yield tmp_path
    server.S.analyzer.stop()
    if server.S.analyzer._thread:
        server.S.analyzer._thread.join(timeout=30)


def call(name, args=None):
    result = operate(name, args)
    assert result['ok'], result
    return result['result']


def wait():
    deadline = time.monotonic()+60
    while call('job_status')['state'] in ('running','stopping'):
        assert time.monotonic() < deadline
        time.sleep(.05)
    return call('job_status')


def demo(tmp_path):
    dest=tmp_path/'cases'
    generate(dest,n=8,steps=5)
    settings=json.loads((dest/'settings.json').read_text())
    call('discover',{'path':str(dest)})
    return dest,settings


def definition(expression='a*exp(-k*x)'):
    return {'id':'decay','expression':expression,'parameters':{'a':{'initial':1,'lower':0},'k':{'initial':.5,'lower':0}}}


def test_generic_defaults_and_validation(isolated):
    errors=Settings().validate()
    assert any('density_field' in e for e in errors)
    assert any('liquid_min' in e for e in errors)
    assert Settings().region_zones is None and Settings().molecule_mass==0
    assert not water_settings().validate()
    assert not operate('validate_settings',{'settings':{'liquid_min':'bad'}})['ok']
    assert not operate('validate_settings',{'settings':{'unknown':1}})['ok']
    assert not operate('no_such_operation')['ok']


@pytest.mark.parametrize('normal',['+x','-x','+y','-y','+z','-z'])
def test_contact_angle_all_substrate_directions(normal):
    theta=np.linspace(0,np.pi/2,35);phi=np.linspace(0,2*np.pi,45)
    t,p=np.meshgrid(theta,phi)
    local=np.column_stack((np.sin(t.ravel())*np.cos(p.ravel()),np.sin(t.ravel())*np.sin(p.ravel()),np.cos(t.ravel())))
    axis='xyz'.index(normal[-1]);tangents=[i for i in range(3) if i!=axis]
    world=np.empty_like(local);world[:,tangents]=local[:,:2];world[:,axis]=local[:,2]*(-1 if normal[0]=='-' else 1)
    world[:,axis]+=3
    result=contact_for_settings(world,Settings(substrate_normal=normal,substrate_position=3))
    assert result.ok
    assert result.angle_deg==pytest.approx(90,abs=1e-6)
    assert result.contact_radius==pytest.approx(1)


@pytest.mark.parametrize('expression',["__import__('os')",'x.__class__','x[0]','[x for x in x]','open(x)','lambda: x','exp(x, x)','2**(2**10000)'])
def test_expression_rejects_python_and_bad_syntax(tmp_path,expression):
    registry=ModelRegistry(tmp_path)
    if expression=='2**(2**10000)':
        registry.register(definition(expression))
        with pytest.raises((ValueError,OverflowError)):
            registry.evaluate('decay',[1])
    else:
        with pytest.raises(ValueError):registry.register(definition(expression))


def test_fit_and_failure_modes(tmp_path):
    registry=ModelRegistry(tmp_path);registry.register(definition())
    x=np.linspace(0,4,50);y=2*np.exp(-.7*x)
    fit=registry.fit('decay',x,y)
    assert fit['status']=='converged' and fit['r2']>.999999
    assert fit['parameters']==pytest.approx({'a':2,'k':.7},rel=1e-5)
    assert registry.fit('decay',[1],[2])['status']=='failed'
    excluded=registry.fit('decay',[*x,math.nan],[*y,math.inf])
    assert excluded['n_excluded']==1 and excluded['status']=='converged'
    registry.register({'id':'domain','expression':'log(x)','parameters':{}})
    assert registry.fit('domain',[-1,0,1],[1,2,3])['status']=='failed'
    assert registry.fit('decay',x,y,max_nfev=1)['status']=='failed'
    registry.register({'id':'nano','expression':'a*exp(-k*x)','parameters':{'a':{'initial':1e-26,'lower':0},'k':{'initial':1e8,'lower':0}}})
    x=np.linspace(0,2e-9,50);fit=registry.fit('nano',x,2e-26*np.exp(-3e8*x))
    assert fit['parameters']==pytest.approx({'a':2e-26,'k':3e8},rel=1e-4,abs=1e-35)


def test_python_extension_contract_and_labels(tmp_path,monkeypatch):
    from mdfoam_droplet_viewer import figures
    class Extension:
        definition={'id':'installed-demo','kind':'python','parameters':{'a':{'initial':2}},'x_quantity':'time','y_quantity':'r_norm'}
        @staticmethod
        def predict(x,parameters,context):return parameters['a']*x+context.get('offset',0)
    class Entry:
        name='test-extension'
        def load(self):return Extension
    monkeypatch.setattr('mdfoam_droplet_viewer.models.importlib.metadata.entry_points',lambda **kw:[Entry()])
    registry=ModelRegistry(tmp_path)
    assert registry.evaluate('installed-demo',[0,1],context={'offset':1})['y']==[1,3]
    fit=registry.fit('installed-demo',[0,1,2],[0,3,6]);assert fit['parameters']['a']==pytest.approx(3)
    built=figures.build({'kind':'xy','series':[{'x':[0,1],'y':[1,2]}],'xlabel':'time [s]','ylabel':'ratio [1]'},figures.Source(tmp_path))
    assert built['plot']['xlabel']=='time [s]' and built['plot']['ylabel']=='ratio [1]'


def test_gui_service_cli_and_storage_agree(isolated):
    dest,settings=demo(isolated)
    info=call('inspect');assert info['candidates']['density']==['count_sample','density_sample'] or set(info['candidates']['density'])=={'density_sample','count_sample'}
    client=TestClient(server.app)
    response=client.post('/api/v1/analyze',json={'settings':settings}).json()
    assert response['ok'],response
    assert wait()['state']=='done'
    rows=call('results')['results']['sample-1']['rows']
    assert all(row['status']=='ok' for row in rows)
    assert all(row['gas_p']>0 for row in rows)
    assert all(row['temp_mean']==pytest.approx(300) for row in rows)
    call('register_model',json.loads((dest/'register.json').read_text()))
    fit=call('fit_model',json.loads((dest/'fit.json').read_text()));assert fit['status']=='converged'
    call('save',{'name':'test'})
    figure=call('export_model_figure',{'fit_id':fit['fit_id'],'output':str(isolated/'fit.png')})
    assert (isolated/'fit.png').read_bytes().startswith(b'\x89PNG')
    cmd=[sys.executable,'-m','mdfoam_droplet_viewer.cli','--root',str(server.CACHE_ROOT),'call','results','--saved','test']
    result=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8',check=True)
    assert json.loads(result.stdout)['result']['results']['sample-1']['rows']==json.loads(json.dumps(rows))
    server.S.models={};call('open',{'name':'test'});assert fit['fit_id'] in server.S.models
    frame=client.post('/api/viz',json={'case':'sample-1','time':rows[0]['time_name'],'settings':settings,'opts':{'grid':True}})
    assert frame.status_code==200 and frame.json()['coordinate_frame']['normal']=='+z'
    for endpoint in ('/api/theory','/api/kelvin','/api/evap'):
        assert client.post(endpoint,json={}).status_code==404
    assert client.get('/').status_code==200
    assert client.post('/api/v1/results',json={},headers={'Origin':'https://example.com'}).status_code==403


def test_missing_fields_cancel_and_unknown_job(isolated):
    _,settings=demo(isolated)
    assert not operate('analyze',{'settings':{**settings,'density_field':'missing'}})['ok']
    job=call('analyze',{'settings':settings})
    call('cancel',{'job_id':job['job_id']});assert wait()['state'] in ('stopped','done')
    assert not operate('job_status',{'job_id':'missing'})['ok']


def test_mcp_stdio_roundtrip(isolated):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    dest=isolated/'mcp-cases';generate(dest,n=6,steps=4)
    async def run():
        params=StdioServerParameters(command=sys.executable,args=['-m','mdfoam_droplet_viewer.mcp_server','--root',str(isolated/'mcp-root')])
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools=await session.list_tools();assert {'schema','analyze','fit_model','export_model_figure'} <= {t.name for t in tools.tools}
                async def tool(name,args=None):
                    result=await session.call_tool(name,args or {})
                    assert not result.isError,result
                    data=json.loads(next(c.text for c in result.content if c.type=='text'))
                    assert data['ok'],data
                    return data['result']
                schema=await tool('schema');assert schema['model']['properties']['expression']
                await tool('discover',{'path':str(dest)})
                settings=json.loads((dest/'settings.json').read_text())
                await tool('analyze',{'settings':settings})
                for _ in range(400):
                    job=await tool('job_status')
                    if job['state'] not in ('running','stopping'):break
                    await asyncio.sleep(.05)
                assert job['state']=='done',job
                await tool('register_model',json.loads((dest/'register.json').read_text()))
                result=await tool('fit_model',json.loads((dest/'fit.json').read_text()))
                assert result['status']=='converged'
                await tool('export_model_figure',{'fit_id':result['fit_id'],'output':str(isolated/'mcp-fit.png')})
                await tool('save',{'name':'mcp-test'})
    asyncio.run(run())
    assert (isolated/'mcp-fit.png').is_file()
