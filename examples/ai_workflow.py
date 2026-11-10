"""Run the public service through the installed CLI: analysis, fit, export."""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    root=args.directory.resolve();root.mkdir(parents=True,exist_ok=True)
    cache=root/'cache';data=root/'data'
    subprocess.run([sys.executable,'-m','mdfoam_droplet_viewer.demo',str(data)],check=True)
    cli=[sys.executable,'-m','mdfoam_droplet_viewer.cli','--root',str(cache)]
    def run(*arguments):
        result=subprocess.run([*cli,*arguments],capture_output=True,text=True,encoding='utf-8')
        if result.returncode:raise RuntimeError(result.stdout+'\n'+result.stderr)
        payload=json.loads(result.stdout)
        if not payload['ok']:raise RuntimeError(payload)
        return payload['result']
    run('analyze',str(data),'--settings',str(data/'settings.json'),'--save','demo','--workers','1','--quiet')
    run('call','register_model','--input',str(data/'register.json'))
    fit=run('call','fit_model','--input',str(data/'fit.json'),'--saved','demo')
    if fit['status']!='converged':raise RuntimeError(fit)
    request=root/'export.json';request.write_text(json.dumps({'fit_id':fit['fit_id'],'output':str(root/'fit.png')}),encoding='utf-8')
    run('call','export_model_figure','--input',str(request),'--saved','demo')
    print(json.dumps({'figure':str(root/'fit.png'),'parameters':fit['parameters'],'r2':fit['r2']},indent=2))


if __name__=='__main__':main()
