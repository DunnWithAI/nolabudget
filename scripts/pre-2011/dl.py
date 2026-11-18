import sys,os,json,time,hashlib,urllib.request,urllib.parse,re
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
ROOT='/Users/andrewdunn/Library/CloudStorage/Dropbox/dev/nolabudget/data/raw'
def fetch(url,retries=12):
    import subprocess
    for i in range(retries):
        tmp='/tmp/.dlpart_%d'%os.getpid()
        r=subprocess.run(['curl','-sS','-L','-m','400','-A',UA,'-o',tmp,'-w','%{http_code}',url],capture_output=True,text=True)
        code=r.stdout.strip()
        if code=='200' and os.path.exists(tmp):
            b=open(tmp,'rb').read(); os.remove(tmp); return b
        print('  err',url[-80:],'HTTP',code,r.stderr[:80],file=sys.stderr,flush=True)
        time.sleep(60 if code in('429','503','502','504') else 10)
    return None
def pages(b):
    m=re.search(rb'/Type\s*/Pages\b[^>]*?/Count\s+(\d+)',b)
    return int(m.group(1)) if m else None
def run(items):
    # items: list of dict(kind='budget'|'acfr', fy, title, doc_type, ts, orig, notes, fname, site)
    for it in items:
        base=os.path.join(ROOT,'pre-2011' if it['kind']=='budget' else 'acfr',str(it['fy']))
        os.makedirs(base,exist_ok=True)
        fn=it.get('fname') or urllib.parse.unquote(it['orig'].split('/')[-1]).replace(' ','_')
        p=os.path.join(base,fn)
        url=it['url'] if 'url' in it else 'https://web.archive.org/web/%sif_/%s'%(it['ts'],it['orig'])
        if not os.path.exists(p):
            b=fetch(url)
            time.sleep(6.0 if 'archive.org' in url else 1.2)
            if b is None: print('FAIL',url); continue
            open(p,'wb').write(b)
        b=open(p,'rb').read()
        ok=b[:5]==b'%PDF-' if p.lower().endswith('.pdf') else True
        print('OK' if ok and len(b)>2000 else 'BAD',len(b),p)
        it['_path']=p
        if not ok or len(b)<2000: os.rename(p,p+'.bad'); continue
        man=os.path.join(base,'..','manifest.json') 
        man=os.path.normpath(man)
        try: M=json.load(open(man))
        except: M=[]
        M=[m for m in M if m['path']!=p]
        M.append(dict(fiscal_year=it['fy'],title=it['title'],doc_type=it['doc_type'],source_site=it.get('site','web.archive.org (cityofno.com)'),url=url,path=p,bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),pages=pages(b) if p.lower().endswith('.pdf') else None,notes=it.get('notes','')))
        json.dump(M,open(man,'w'),indent=1)
