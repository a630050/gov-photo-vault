from .models import Membership,Department

CORE_CAPABILITIES=('upload','organize','download','forms','export','analyze')
PEOPLE_CAPABILITIES=('people_view','face_search','people_manage')
CAPABILITIES=CORE_CAPABILITIES+PEOPLE_CAPABILITIES
def effective(m):
    if m.user.is_superuser:return {**dict.fromkeys(CORE_CAPABILITIES,True),**{k:m.permissions.get(k,False) for k in PEOPLE_CAPABILITIES}}
    normal=m.role in ('member','supervisor')
    defaults={'upload':normal,'organize':normal,'download':normal,'forms':m.role=='supervisor','export':False,'analyze':normal,**dict.fromkeys(PEOPLE_CAPABILITIES,False)}
    return {key:m.permissions.get(key,defaults[key]) for key in CAPABILITIES}
def allowed(user,department,key):
    if not user.is_authenticated or not user.is_active:return False
    if user.is_superuser and key not in PEOPLE_CAPABILITIES:return True
    member=Membership.objects.filter(user=user,department_id=department).first()
    return bool(member and effective(member).get(key,False))
def memberships_data():
    return [dict(id=m.id,user_id=m.user_id,department_id=m.department_id,role=m.role,permissions=effective(m)) for m in Membership.objects.select_related('user').all()]
def capabilities_data(user):
    if user.is_superuser:
        granted={m.department_id:effective(m) for m in Membership.objects.filter(user=user)}
        return {str(d.id):granted.get(d.id,{**dict.fromkeys(CORE_CAPABILITIES,True),**dict.fromkeys(PEOPLE_CAPABILITIES,False)}) for d in Department.objects.all()}
    return {str(m.department_id):effective(m) for m in Membership.objects.filter(user=user)}
