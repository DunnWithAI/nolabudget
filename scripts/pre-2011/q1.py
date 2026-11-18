import sys; sys.path.insert(0,'.')
from cdx import cdx
import re
r=cdx('nola.gov/*',['mimetype:application/pdf'],limit=5000,extra='&from=2010&to=2012')
print(len(r))
for x in r:
    if re.search(r'udget|apital|CAFR|inanc|ordinance',x[1],re.I): print(*x[:2],x[4])
