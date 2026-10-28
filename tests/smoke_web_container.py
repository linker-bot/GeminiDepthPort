"""Real HTTP smoke test inside the Web image; no Docker socket or USB device required."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler


def main():
    root=Path(__file__).resolve().parents[1]
    inventory=root/'inventory.json'; original=inventory.read_bytes()
    host = '0.0.0.0' if os.environ.get('ORBBEC_CONTAINERIZED') == '1' else '127.0.0.1'
    process=subprocess.Popen([sys.executable,'-u',str(root/'web_server.py'),'--host',host,'--port','0'],
                             cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    try:
        line=process.stdout.readline()
        match=re.search(r'http://127\.0\.0\.1:(\d+)',line)
        if not match: raise RuntimeError('Web failed to start: '+line+process.stdout.read())
        base='http://127.0.0.1:'+match.group(1)
        def request(path,body=None,headers=None):
            data=None if body is None else json.dumps(body).encode()
            with build_opener(ProxyHandler({})).open(Request(base+path,data=data,headers=headers or {}),timeout=20) as result:
                return result.read()
        assert json.loads(request('/healthz'))['status']=='ok'
        html=request('/').decode();assert 'GeminiDepthPort' in html
        token=re.search(r'name="csrf-token" content="([^"]+)"',html).group(1)
        state=json.loads(request('/api/state'))
        assert {c['name'] for c in state['inventory']['cameras']}=={'camera_a','camera_b','camera_c'}
        headers={'Content-Type':'application/json','X-CSRF-Token':token,'Origin':base}
        # An unchanged save still exercises atomic replace and the shared writable control lock.
        payload={'revision':state['revision'],'cameras':[{'name':c['name'],'parameters':{}} for c in state['inventory']['cameras']]}
        assert 'message' in json.loads(request('/api/config',payload,headers))
        for path,body,request_headers,expected in [('/api/job',{'action':'shell'},headers,400),
                ('/api/config',payload,{'Content-Type':'application/json'},403),
                ('/api/state',None,{'Host':'evil.example'},403)]:
            try:
                request(path,body,request_headers)
                raise AssertionError('Request was not rejected')
            except HTTPError as exc: assert exc.code==expected
        print('Container HTTP smoke passed: health, page, state, atomic config save, action/CSRF/Host guards.')
    finally:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        inventory.write_bytes(original)

if __name__=='__main__':main()
