import json,os,re,time,hashlib,struct,urllib.request,sys
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
B="https://archives-nolalibrary.contentdm.oclc.org"
ROOT='/Users/andrewdunn/Library/CloudStorage/Dropbox/dev/nolabudget/data/raw/pre-2011'
items=json.load(open('cdm_items.json'))
KEEP=lambda t: re.search(r'Annual Report of the Mayor|Budget of the Probable|Annual Message of the Mayor|Report of Operations',t)
def jpeg_size(b):
    i=2
    while i<len(b):
        if b[i]!=0xFF: i+=1; continue
        m=b[i+1]
        if m in (0xC0,0xC1,0xC2): h,w=struct.unpack('>HH',b[i+5:i+9]); return w,h
        l=struct.unpack('>H',b[i+2:i+4])[0]; i+=2+l
def mkpdf(jpgs,path):
    objs=[];
    n=len(jpgs); kids=[]
    out=bytearray(b'%PDF-1.4\n'); offs=[]
    def add(body):
        offs.append(len(out)); out.extend(b'%d 0 obj\n'%(len(offs))+body+b'\nendobj\n'); return len(offs)
    cat=add(b'<< /Type /Catalog /Pages 2 0 R >>')
    pages_id=add(b'')  # placeholder
    pid=[]
    for jb in jpgs:
        w,h=jpeg_size(jb)
        img=add(b'<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n'%(w,h,len(jb))+jb+b'\nendstream')
        # colorspace may be gray
        content=b'q %d 0 0 %d 0 0 cm /Im0 Do Q'%(w,h)
        cs=add(b'<< /Length %d >>\nstream\n'%len(content)+content+b'\nendstream')
        pg=add(b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>'%(w,h,img,cs))
        pid.append(pg)
    body=b'<< /Type /Pages /Kids [%s] /Count %d >>'%(b' '.join(b'%d 0 R'%p for p in pid),len(pid))
    # rewrite object 2 by appending replacement is complex; rebuild instead
    return pid,body
def build(jpgs,path):
    out=bytearray(b'%PDF-1.4\n'); offs={}
    def w(i,body): offs[i]=len(out); out.extend(b'%d 0 obj\n'%i+body+b'\nendobj\n')
    nxt=3; pids=[]; 
    w(1,b'<< /Type /Catalog /Pages 2 0 R >>')
    for jb in jpgs:
        wd,ht=jpeg_size(jb)
        comps=jb.find(b'\xff\xc0'); 
        # components count
        m=re.search(rb'\xff[\xc0\xc1\xc2]..(.)(..)(..)(.)',jb,re.S); nc=m.group(4)[0] if m else 3
        cs=b'/DeviceGray' if nc==1 else b'/DeviceRGB'
        img,con,pg=nxt,nxt+1,nxt+2; nxt+=3
        w(img,b'<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n'%(wd,ht,cs,len(jb))+jb+b'\nendstream')
        c=b'q %d 0 0 %d 0 0 cm /Im0 Do Q'%(wd,ht)
        w(con,b'<< /Length %d >>\nstream\n'%len(c)+c+b'\nendstream')
        w(pg,b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>'%(wd,ht,img,con))
        pids.append(pg)
    w(2,b'<< /Type /Pages /Kids [%s] /Count %d >>'%(b' '.join(b'%d 0 R'%p for p in pids),len(pids)))
    xr=len(out); out.extend(b'xref\n0 %d\n0000000000 65535 f \n'%nxt)
    for i in range(1,nxt): out.extend(b'%010d 00000 n \n'%offs[i])
    out.extend(b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n'%(nxt,xr))
    open(path,'wb').write(out); return bytes(out)
def fy_of(date,title):
    ys=[int(y) for y in re.findall(r'(1[89]\d\d|20\d\d)',date or '')]
    return max(ys) if ys else None
mp=ROOT+'/manifest.json'
M=json.load(open(mp))
for it in items:
    if not KEEP(it['title']): continue
    fy=fy_of(it['date'],it['title'])
    if fy is None: print('nofy',it); continue
    slug=re.sub(r'[^A-Za-z0-9]+','_',it['title'].strip())[:50].strip('_')
    d=ROOT+'/%d'%fy; os.makedirs(d,exist_ok=True)
    path=d+'/NOPL_CityArchives_%s_%s_id%s.pdf'%(slug,re.sub(r'[^0-9A-Za-z]+','-',it['date'].strip()),it['id'])
    if os.path.exists(path): continue
    from concurrent.futures import ThreadPoolExecutor
    def getp(p):
        u=B+'/digital/iiif/p16880coll90/%s/full/1800,/0/default.jpg'%p
        for t in range(4):
            try: return urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':UA}),timeout=120).read()
            except Exception as e:
                print('err',u,e,flush=True); time.sleep(3)
                if '501' in str(e) or '400' in str(e): u=u.replace('/full/1800,/','/full/full/')
        return None
    with ThreadPoolExecutor(4) as ex: jp=list(ex.map(getp,it['pages']))
    if None in jp: jp=[]
    if len(jp)!=len(it['pages']) or not jp: print('incomplete',it['id'],flush=True); continue
    b=build(jp,path)
    M=[m for m in M if m['path']!=path]
    M.append(dict(fiscal_year=fy,title=it['title'].strip()+' (%s)'%it['date'].strip(),doc_type='mayors_annual_report' if 'Annual Report' in it['title'] else ('budget_ordinance_proposal' if 'Budget' in it['title'] else 'city_publication'),source_site='New Orleans Public Library City Archives CONTENTdm (archives-nolalibrary.contentdm.oclc.org)',url=B+'/digital/collection/p16880coll90/id/%s (pages via IIIF /digital/iiif/p16880coll90/<pageptr>/full/1800,/0/default.jpg, ptrs %s..%s)'%(it['id'],it['pages'][0],it['pages'][-1]),path=path,bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),pages=len(jp),notes='Scanned page images (1800px wide) assembled into PDF by downloader; no OCR text layer. Call number: %s. Folder year = latest year in date label "%s".'%(it['call'],it['date'].strip())))
    json.dump(M,open(mp,'w'),indent=1)
    print('OK',path,len(b),flush=True)
