import sys,re; sys.path.insert(0,'.')
from cdx import cdx
frm,to=sys.argv[1],sys.argv[2]
for u in sys.argv[3:]:
    r=cdx(u,['statuscode:200'],limit=3000,extra='&from=%s&to=%s'%(frm,to))
    print('==',u,len(r))
    for x in r:
        if not re.search(r'\.(jpg|gif|png|css|js|ico)\b',x[1],re.I) and re.search(r'udget|apital|CAFR|inanc|[Oo]rdinance|\.ashx|pdf',x[1]+x[2]): print(*x[:3],x[4])
