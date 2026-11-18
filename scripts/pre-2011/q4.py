import sys,re; sys.path.insert(0,'.')
from cdx import cdx
import urllib.request,json,time
UA="Mozilla/5.0"
for u in ['cityofno.com/pg-35-97*','cityofno.com/pg-35-*udget*','cityofno.com/pg-35-9*']:
    q='https://web.archive.org/cdx/search/cdx?output=json&fl=timestamp,original,statuscode,length&limit=200&url='+u
    try:
        r=json.loads(urllib.request.urlopen(urllib.request.Request(q,headers={'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36'}),timeout=100).read())[1:]
    except Exception as e: print(u,e); continue
    print('==',u,len(r))
    for x in r[:80]: print(*x)
    time.sleep(1.5)
