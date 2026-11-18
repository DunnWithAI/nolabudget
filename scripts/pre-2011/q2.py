import re
import sys; sys.path.insert(0,'.')
from cdx import cdx
for u in sys.argv[1:]:
    r=cdx(u,['statuscode:200'],limit=2000,extra='&from=2009&to=2013')
    print('==',u,len(r))
    for x in r:
        if not re.search(r'\.(jpg|gif|png|css|js|ico)',x[1],re.I): print(*x[:3],x[4])
import re
