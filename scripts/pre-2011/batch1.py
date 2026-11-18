import sys; sys.path.insert(0,'.')
from dl import run
P='http://cityofno.com/Portals/'
def B(fy,title,dt,ts,path,notes='',host=None,**k):
    o=(host or 'http://cityofno.com')+('/'+path if path.startswith('portals/') else '/Portals/'+path)
    o=o.replace("'",'%27')
    return dict(kind='budget',fy=fy,title=title,doc_type=dt,ts=ts,orig=o,notes=notes,**k)
items=[
 B(2009,'2009 Operating Budget (Proposed)','proposed_operating_budget','20100105045151','Portal35/Resources/CNO_2009_Operating_Budget-Proposed1.pdf','Mayor Nagin proposed 2009 operating budget book'),
 B(2008,'2008 Adopted Budget Book','adopted_operating_budget','20100105044855','Portal35/Resources/CNO_BudgetBook2008Adopted3.pdf','Linked on city site as 2008 budget book'),
 B(2009,'2009 Executive Capital Budget','proposed_capital_budget','20100105044546','Portal35/Resources/Excapitalbudget2009.pdf'),
 B(2010,'2010-2014 Executive Capital Budget','proposed_capital_budget','20100103194851','PublicAdvocacy/Resources/2010-2014%20Executive%20Capital%20Budget.pdf'),
 B(2010,'2010 Budget Presentation FINAL','budget_presentation','20100103191114','PublicAdvocacy/Resources/2010%20Budget%20Presentation%20FINAL.pdf'),
 B(2010,'2010 Budget Presentation - Mayors Office','budget_presentation','20100104000154',"portals/PublicAdvocacy/Resources/2010_Budget_Presentation_Mayor's_Office_v.pdf"),
 B(2010,'2010 Budget Media Briefing 12/17/09','budget_presentation','20100103195128','PublicAdvocacy/Resources/2010_budget_media_briefing_121709.pdf'),
 B(2010,'2010 Budget Handout','budget_summary','20100103195921','PublicAdvocacy/Resources/2010_budget_handout_jdrcdq.pdf'),
 B(2010,'Mayor Nagin statement on 2010 budget (press release 2009-12-06)','press_release','20100103223640','NOHSEP/Resources/PR%20100609%20STATEMENT%20FROM%20MAYOR%20NAGIN%20REGARDING%202010%20BUDGET.pdf'),
 B(2010,'Departmental Letters on Budget Impact','budget_supporting','20100103171835','PublicAdvocacy/Resources/Departmental_Letters_on_Budget_Impact.pdf'),
 B(2010,'Response to OIG Review of Budget Process (13 Nov 2009)','budget_supporting','20100103183203','PublicAdvocacy/Resources/Response_to_OIG_%20Review_of%20_Budget_Process%20-%20OIG_I-E%2009002%20-%2013Nov09.pdf'),
 B(2010,'Katrina Budget Impact','budget_supporting','20100103234754','PublicAdvocacy/Resources/Katrina%20Budget%20Impact.pdf'),
 B(2009,'2009 Budget Presentation (Mayor, Budget_2009_Presentation)','budget_presentation','20100103184443','PublicAdvocacy/Resources/Budget_2009_Presentation.pdf'),
 B(2009,'2009 Budget Presentation (Budget2009.pdf)','budget_presentation','20081203092126','Portal35/Resources/Budget2009.pdf',host='http://www.cityofno.com:80'),
 B(2009,'2009 ORDA budget presentation','budget_presentation','20081209012850','ORM/Resources/2009ORDAbudgetpresentation.pdf',host='http://www.cityofno.com'),
 B(2009,'2009 Budget Hearing Schedule','budget_supporting','20081203030659','PublicAdvocacy/Resources/2008-1118-BudgetHearingSchedule.pdf',host='http://www.cityofno.com:80'),
 B(2009,'2009 Budget - City Council','departmental_budget_presentation','20081203030703','PublicAdvocacy/Resources/2009%20Budget%20City%20Council.pdf',host='http://www.cityofno.com:80'),
 B(2009,'2009 Human Services Budget','departmental_budget_presentation','20081203030710','PublicAdvocacy/Resources/2009%20Human%20Services%20Budget.pdf',host='http://www.cityofno.com:80'),
 B(2009,'CPA Budget Hearing Presentation 10 Nov 2008','departmental_budget_presentation','20081203031300','PublicAdvocacy/Resources/CPA_Budget%20Hearing%20Presentation10nov08.pdf',host='http://www.cityofno.com:80'),
 B(2009,'DPW 2009 Budget Hearing Presentation','departmental_budget_presentation','20081203031314','PublicAdvocacy/Resources/DPW_2009%20Budget%20Hearing%20Presentation_FINAL.pdf',host='http://www.cityofno.com:80'),
 B(2009,'Parks and Parkways 2009 Budget Presentation','departmental_budget_presentation','20081203031606','PublicAdvocacy/Resources/Parks%20and%20Parkways%202009%20Budget%20Presentation.pdf',host='http://www.cityofno.com:80'),
 B(2009,'Millages Affects','budget_supporting','20081203092126','Portal35/Resources/MillagesAffects.pdf',host='http://www.cityofno.com'),
 B(2009,'Roll vs No Roll Forward of Millage','budget_supporting','20081203092126','Portal35/Resources/RollvsNoRollForwardofMillage.pdf',host='http://www.cityofno.com'),
 B(2008,'Revenue Estimating Conference Report 2008-04-30','revenue_estimate','20100105202747','Intelliport/Resources/RevenueEstimatingConferenceReport20080430.pdf'),
 B(2007,"2007 Mayor's Budget Overview Presentation",'budget_presentation','20100105045826',"Portal35/Resources/2007%20MAYOR'S%20BUDGET%20OVERVIEW%20PRESENTATION%20final%20%5BCompatibility%20Mode%5D.pdf"),
 B(2008,"2008 Mayor's Budget Presentation",'budget_presentation','20100103215525',"PublicAdvocacy/Resources/2008_mayor's_budget_presentation.pdf"),

 B(2008,'2008 Mayor Official Budget Presentation (PowerPoint)','budget_presentation','20100105044317','Portal35/Resources/2008mayor_official_budget_presentation.ppt',fname='2008mayor_official_budget_presentation.ppt'),
]
run(items)
