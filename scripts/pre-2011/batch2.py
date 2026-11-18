import sys; sys.path.insert(0,'.')
from dl import run
def B(fy,title,dt,ts,path,notes='',host='http://cityofno.com',**k):
    o=host+('/'+path if path.lower().startswith(('portals/','resources/')) else '/Portals/'+path)
    return dict(kind='budget',fy=fy,title=title,doc_type=dt,ts=ts,orig=o.replace("'",'%27'),notes=notes,**k)
run([
 B(2010,'2010 Budget Reductions (12-1-09)','budget_supporting','20100103171835','PublicAdvocacy/Resources/2010_reductions_12-1-09.pdf'),
 B(2010,'2010 Budget Hearing Schedule Notice (Nov 2009)','budget_supporting','20100103171835','PublicAdvocacy/Resources/Hearing_Schedule_Notice_2010_11_09.pdf'),
 B(2010,'2010 Budget Presentation - D-CDBG (Recovery) 11.16.09 (PowerPoint pptx)','departmental_budget_presentation','20100103193111','portals/PublicAdvocacy/Resources/2010_Budget_Presentation_D-CDBG_V4_FINAL_%2011.16.09.pptx'),
 B(2009,'2009 Cost Reduction Variance','budget_supporting','20090205014215','Intelliport/Resources/2009%20cost%20reduction%20variance.pdf',host='http://www.cityofno.com:80'),
 B(2004,'2004 Bond Program Overview (Portal1)','capital_bond_program','20041101000000','resources/portal1/bond_overview.pdf',host='http://www.cityofno.com:80'),
 B(2005,'Rescope Budget spreadsheet (xls)','capital_budget_supporting','20050505152835','Resources/rescopeBudget.xls',host='http://www.cityofno.com:80',fname='rescopeBudget.xls'),
])
