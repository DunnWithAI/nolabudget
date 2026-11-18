import sys,re,json,time,urllib.request,urllib.parse
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
def q(url,extra='',lim=3000):
    u='https://web.archive.org/cdx/search/cdx?output=json&collapse=urlkey&fl=timestamp,original,mimetype,statuscode,length&limit=%d&url=%s%s'%(lim,urllib.parse.quote(url,safe='*/:'),extra)
    for i in range(3):
        try:
            r=json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':UA}),timeout=100).read())[1:]; time.sleep(1.5); return r
        except Exception as e: print('err',url,e); time.sleep(5)
    return []
for url in sys.argv[1:]:
    r=q(url,'&filter=mimetype:application/pdf&filter=statuscode:200')
    print('==',url,len(r))
    for x in r:
        if re.search(r'udget|apital|ppropriation|illage',x[1],re.I): print(*x[:2],x[4])
