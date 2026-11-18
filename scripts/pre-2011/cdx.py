import sys,json,time,urllib.request,urllib.parse
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
def cdx(url,filters=(),limit=5000,extra=''):
    q='https://web.archive.org/cdx/search/cdx?output=json&collapse=urlkey&fl=timestamp,original,mimetype,statuscode,length&limit=%d&url=%s'%(limit,urllib.parse.quote(url,safe='*/:'))
    for f in filters: q+='&filter='+urllib.parse.quote(f,safe=':')
    q+=extra
    for i in range(3):
        try:
            r=urllib.request.urlopen(urllib.request.Request(q,headers={'User-Agent':UA}),timeout=150).read().decode()
            time.sleep(1.3)
            return json.loads(r)[1:]
        except Exception as e:
            print('err',e,file=sys.stderr); time.sleep(8)
    return []
if __name__=='__main__':
    url=sys.argv[1]; flt=sys.argv[2:]
    for r in cdx(url,flt): print(*r)
