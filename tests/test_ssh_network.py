"""Exercise the actual SSH/SFTP transport against a loopback-only test server."""
import os
import posixpath
import socket
import threading
import time
from pathlib import Path

import paramiko

from mdfoam_droplet_viewer.demo import generate
from mdfoam_droplet_viewer.remote import RemoteConfig, RemoteSession


class Authentication(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if (username,password)==('test','fixture-password') else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'password'

    def check_channel_request(self, kind, channel_id):
        return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class Files(paramiko.SFTPServerInterface):
    def __init__(self, server, root):
        super().__init__(server)
        self.root=Path(root).resolve()

    def path(self,path):
        resolved=(self.root / path.lstrip('/')).resolve()
        if not resolved.is_relative_to(self.root):
            raise PermissionError(path)
        return resolved

    def canonicalize(self,path):
        return posixpath.normpath('/'+path.lstrip('/'))

    def list_folder(self,path):
        try:
            result=[]
            for p in self.path(path).iterdir():
                attr=paramiko.SFTPAttributes.from_stat(p.stat());attr.filename=p.name;result.append(attr)
            return result
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno or 2)

    def stat(self,path):
        try:return paramiko.SFTPAttributes.from_stat(self.path(path).stat())
        except OSError as exc:return paramiko.SFTPServer.convert_errno(exc.errno or 2)

    lstat=stat

    def open(self,path,flags,attr):
        if flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT):return paramiko.SFTP_PERMISSION_DENIED
        try:
            handle=paramiko.SFTPHandle(flags);handle.readfile=self.path(path).open('rb');return handle
        except OSError as exc:return paramiko.SFTPServer.convert_errno(exc.errno or 2)


def test_real_ssh_scan_and_mirror(tmp_path,monkeypatch):
    generate(tmp_path/'remote/cases',n=4,steps=3)
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(4);sock.settimeout(.2)
    port=sock.getsockname()[1];key=paramiko.RSAKey.generate(2048)
    hosts=tmp_path/'known_hosts';hosts.write_text(f'[127.0.0.1]:{port} {key.get_name()} {key.get_base64()}\n')
    original=paramiko.SSHClient.load_system_host_keys
    def load(self,filename=None):
        original(self,filename)
        self.load_host_keys(str(hosts))
    monkeypatch.setattr(paramiko.SSHClient,'load_system_host_keys',load)
    stop=threading.Event();transports=[]
    def serve():
        while not stop.is_set():
            try:client,_=sock.accept()
            except socket.timeout:continue
            except OSError:break
            transport=paramiko.Transport(client);transports.append(transport)
            transport.add_server_key(key)
            transport.set_subsystem_handler('sftp',paramiko.SFTPServer,Files,root=tmp_path/'remote')
            transport.start_server(server=Authentication())
    thread=threading.Thread(target=serve,daemon=True);thread.start()
    session=None
    try:
        session=RemoteSession(RemoteConfig('127.0.0.1',port,'test'),password='fixture-password')
        session._max_conn=1
        cases=session.find_cases('/cases','auto')
        assert {c['name'] for c in cases}=={'sample-1','sample-2'}
        mirrored=session.mirror_cases(cases,tmp_path/'mirror',['density_sample','T_sample'],False,bulk=False)
        assert len(mirrored)==2
        assert list((tmp_path/'mirror').rglob('density_sample'))
        assert list((tmp_path/'mirror').rglob('owner'))
    finally:
        if session:session.close()
        stop.set();sock.close()
        for transport in transports:transport.close()
        thread.join(timeout=2)
