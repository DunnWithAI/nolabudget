import json,os,shutil,hashlib,subprocess,re
ROOT='/Users/andrewdunn/Library/CloudStorage/Dropbox/dev/nolabudget/data/raw/acfr'
d=json.load(open('lla_no.json'))
urls={}
for o in d:
    for u in o['links']:
        urls[o['date'][6:]+o['date'][:2]+o['date'][3:5]+'_'+o['acn']+'_'+u.split('/')[-1]]=(u,o)
M={'20010926_01501620_40ae7b50.pdf':(2000,'CAFR'),'20020821_02902137_40bcedb0.pdf':(2001,'CAFR'),'20030917_03802000_40be246a.pdf':(2002,'CAFR'),'20040922_04702954_0000007e.pdf':(2003,'CAFR'),'20050720_05302470_00000dfc.pdf':(2004,'CAFR (first issue, LLA 2005-07-20)'),'20050907_06900572_00000f6e.pdf':(2004,'CAFR (second LLA posting 2005-09-07)'),'20070801_07500692_000013c0.pdf':(2005,'Basic Financial Statements'),'20080116_07702652_00003b12.pdf':(2006,'Basic Financial Statements'),'20080924_72071600_00004f3b.pdf':(2007,'Basic Financial Statements'),'20100106_72081958_0001493f.pdf':(2008,'Basic Financial Statements'),'20101208_72093922_0001b030.pdf':(2009,'Basic Financial Statements'),'20111005_72102460_00022404.pdf':(2010,'Basic Financial Statements'),'20121024_72112403_0002d2d4.pdf':(2011,'Basic Financial Statements'),'20130807_72122154_0003452b.pdf':(2012,'Basic Financial Statements'),'20140827_72130823_000029ff.pdf':(2013,'CAFR'),'20150812_72142246_0000973b.pdf':(2014,'CAFR'),'20160720_72152156_00010047.pdf':(2015,'CAFR'),'20170802_72162154_000156b1.pdf':(2016,'CAFR'),'20180801_72172045_0001a34e.pdf':(2017,'CAFR'),'20190807_72181872_0001dc96.pdf':(2018,'CAFR'),'20210728_72191855_000244a5.pdf':(2019,'ACFR'),'20220727_72201855_0002789a.pdf':(2020,'ACFR'),'20230215_72212268_00000bf0.pdf':(2021,'ACFR'),'20230816_72221238_00002728.pdf':(2022,'ACFR'),'20240710_72231365_00005141.pdf':(2023,'ACFR'),'20251008_72241220_00008d0d.pdf':(2024,'ACFR'),'20260701_72250864_0000ae9c.pdf':(2025,'ACFR'),
'20040701_00201278_408479b2.pdf':(1999,'CAFR'),'20040701_99901241_40d216dc.pdf':(1998,'CAFR'),'20040701_98501408_40cf4596.pdf':(1997,'CAFR'),'20040701_97801510_40ce2946.pdf':(1996,'CAFR/single audit package (scan begins mid-document; title page not captured)'),'20040701_96901529_40c876a3.pdf':(1995,'CAFR/single audit package (scan begins mid-document; title page not captured)'),
'20251105_72234443_0000904f.pdf':(2023,'Single Audit Report'),'20251112_72244435_0000912c.pdf':(2024,'Single Audit Report'),'20260715_72254435_0000af60.pdf':(2025,'Single Audit Report'),'20251015_70250060_00008d9c.pdf':(2025,'LLA Budget Deficit Analysis advisory (Oct 2025)')}
info={e['path'].split('/')[-1]:e for e in json.load(open('lla_info_all.json'))}
manp=ROOT+'/manifest.json'
man=json.load(open(manp))
for fn,(fy,kind) in M.items():
    src='lla/'+fn
    if not os.path.exists(src): print('missing',fn); continue
    b=open(src,'rb').read()
    assert b[:5]==b'%PDF-'
    dst=ROOT+'/%d/LLA_%s'%(fy,fn)
    os.makedirs(os.path.dirname(dst),exist_ok=True); shutil.copyfile(src,dst)
    u,o=urls[fn]
    man=[m for m in man if m['path']!=dst]
    dt='single_audit' if 'Single' in kind else ('lla_advisory' if 'advisory' in kind else 'acfr')
    man.append(dict(fiscal_year=fy,title='City of New Orleans FY%d %s (Louisiana Legislative Auditor posting)'%(fy,kind),doc_type=dt,source_site='lla.la.gov (Legislative Auditor Public Reports)',url=u,path=dst,bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),pages=(info.get(fn) or {}).get('pages'),notes='LLA issue date %s, ACN %s; agency "%s"'%(o['date'],o['acn'],o['agency'])))
json.dump(man,open(manp,'w'),indent=1)
print(len(man))
