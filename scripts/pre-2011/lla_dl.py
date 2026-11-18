import json,os,time,urllib.request
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
d=json.load(open('lla_no.json'))
sel=[o for o in d if o['agency'] in('City of New Orleans','City of New Orleans - Single Audit','City of New Orleans - Budget Deficit Analysis')]
for o in sel:
    for u in o['links']:
        fn='lla/%s_%s_%s'%(o['date'][6:]+o['date'][:2]+o['date'][3:5],o['acn'],u.split('/')[-1])
        if os.path.exists(fn): continue
        try:
            b=urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':UA}),timeout=120).read()
            open(fn,'wb').write(b); print(fn,len(b),b[:5],flush=True)
        except Exception as e: print('ERR',u,e,flush=True)
        time.sleep(1.2)
