import json,subprocess,os,sys
R='/Users/andrewdunn/Library/CloudStorage/Dropbox/dev/nolabudget/data/raw'
for k in ['pre-2011','acfr']:
    mp=R+'/'+k+'/manifest.json'; M=json.load(open(mp))
    todo=[m for m in M if m['path'].lower().endswith('.pdf') and not m.get('pages')]
    for i in range(0,len(todo),8):
        chunk=todo[i:i+8]
        out=subprocess.run(['../tools/pdfinfo']+[m['path'] for m in chunk],capture_output=True,text=True,env=dict(os.environ,NP='0'),cwd=os.getcwd()).stdout
        try: info={e['path']:e['pages'] for e in json.loads(out)}
        except Exception as e: print('fail',e,out[:200]); continue
        for m in chunk:
            if isinstance(info.get(m['path']),int): m['pages']=info[m['path']]
    M.sort(key=lambda m:(m['fiscal_year'],m['path']))
    json.dump(M,open(mp,'w'),indent=1)
    print(k,len(M),'null pages:',[m['path'][-40:] for m in M if m['pages'] is None])
