import sys,re,time,urllib.request
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
for spec in sys.argv[1:]:
    ts,u=spec.split(',',1)
    for i in range(4):
        try:
            t=urllib.request.urlopen(urllib.request.Request('https://web.archive.org/web/%s/%s'%(ts,u),headers={'User-Agent':UA}),timeout=90).read().decode('utf8','ignore'); break
        except Exception as e:
            print('err',e,flush=True); t=None; time.sleep(120 if '429' in str(e) else 10)
    time.sleep(6)
    if not t: continue
    print('=====',ts,u)
    seen=set()
    for m in re.finditer(r'href="([^"]+)"[^>]*>(.*?)</a>',t,re.S):
        h=m.group(1)
        if re.search(r'Resources/|\.pdf|\.xls|\.ppt|\.doc|udget',h,re.I) and h not in seen:
            seen.add(h); print(re.sub(r'^/web/\d+/','',h),'|',re.sub(r'\s+',' ',re.sub('<[^>]+>','',m.group(2)))[:80])
