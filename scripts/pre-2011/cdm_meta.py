import json,urllib.request,time
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
B="https://archives-nolalibrary.contentdm.oclc.org"
def get(u):
    return json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':UA}),timeout=60).read())
ids=set()
for term in ['report','annual','budget','message','estimate','ordinance','appropriation','finance','tax','expenditure']:
    d=get(B+'/digital/api/search/collection/p16880coll90/searchterm/%s/field/all/mode/all/conn/and/order/title/ad/asc/maxRecords/200'%term)
    for r in d.get('items',[]): ids.add((r['itemId'].split('/')[-1] if 'itemId' in r else r['itemId'],r['title'].replace('\n',' ')))
    time.sleep(0.5)
out=[]
for i,t in sorted(ids,key=lambda x:int(x[0])):
    m=get(B+'/digital/bl/dmwebservices/index.php?q=dmGetItemInfo/p16880coll90/%s/json'%i)
    try: c=get(B+'/digital/bl/dmwebservices/index.php?q=dmGetCompoundObjectInfo/p16880coll90/%s/json'%i)
    except Exception as e: c={}
    pg=c.get('page') if isinstance(c,dict) else None
    out.append(dict(id=i,title=t,date=m.get('date'),call=m.get('call'),npages=len(pg) if pg else 1,pages=[p['pageptr'] for p in pg] if pg else [i]))
    print(i,m.get('date'),t[:60],len(pg) if pg else 1,flush=True)
    time.sleep(0.5)
json.dump(out,open('cdm_items.json','w'))
