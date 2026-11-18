import sys,re,json,time,urllib.request,urllib.parse
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
def q(url,extra='',lim=3000):
    u='https://web.archive.org/cdx/search/cdx?output=json&collapse=urlkey&fl=timestamp,original,mimetype,statuscode,length&limit=%d&url=%s%s'%(lim,urllib.parse.quote(url,safe='*/:'),extra)
    for i in range(2):
        try:
            r=json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':UA}),timeout=50).read())[1:]; time.sleep(1.5); return r
        except Exception as e: print('err',str(e)[:40]); time.sleep(3)
    return []
seen={}
for y in range(2001,2011):
    r=q('cityofno.com/*','&filter=original:.*[Bb][Uu][Dd][Gg][Ee][Tt].*&filter=statuscode:200&from=%d&to=%d'%(y,y))
    for x in r:
        if x[2] in('image/gif','image/jpeg','image/png','text/css'): continue
        seen[x[1]]=x
    print(y,len(r),flush=True)
for k,x in sorted(seen.items()): print(x[0],x[1][:170],x[2],x[4])
