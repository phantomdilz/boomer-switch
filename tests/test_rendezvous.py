import ipaddress
import uuid

import pytest

from boomer_switch.home_rendezvous import HomeRendezvous, HomeUnavailable, OperationConflict
from boomer_switch.registration import RegistrationStore, AccessDenied


A = str(ipaddress.IPv4Address(134744072))
B = str(ipaddress.ip_address(A)+1)
CELL = str(ipaddress.ip_address(A)+2)
LIGHT = {'light_id':'selected-lamp','name':'Selected lamp','room':'Guest room'}
KEY = 'k'*40
AGENT = str(uuid.UUID(int=1))


def env(peer=A, **extra):
    return {'boomer.direct_peer':True,'wsgi.url_scheme':'https','REMOTE_ADDR':peer,'HTTP_AUTHORIZATION':'Bearer '+KEY,**extra}


@pytest.fixture
def house(tmp_path):
    clock = [1000.0]
    store = RegistrationStore(tmp_path,mode='production',clock=lambda:clock[0])
    raw = store.browser(store.invitation()['token'])
    store.start(raw,'Guest','guest@example.invalid')
    store.verify(raw,store.mailbox('guest@example.invalid')['messages'][-1]['code'])
    store.operator('approve','guest@example.invalid')
    rendezvous = HomeRendezvous(store,KEY)
    rendezvous.access(raw,env())
    return rendezvous,store,raw,clock


def poll(rv, sequence, peer=A, **extra):
    return rv.machine(env(peer),'/home/poll',{'agent_id':AGENT,'sequence':sequence,'request_id':str(uuid.uuid4()),'light':LIGHT,**extra})


def queue(rv,raw,action='read',peer=A,operation_id=None):
    return rv.enqueue(raw,env(peer),{'operation_id':operation_id or str(uuid.uuid4()),'action':action})


def bound(assignment,sequence):
    return {'agent_id':AGENT,'sequence':sequence,'request_id':str(uuid.uuid4()),'operation_id':assignment['operation_id'],'generation':assignment['generation']}


def test_ordinary_read_command_and_current_permission_checks(house):
    rv,store,raw,clock = house
    assert poll(rv,1)['poll_after']==1
    assert rv.access(raw,env()) is True
    read = queue(rv,raw)
    assignment = poll(rv,2)['operation']
    assert assignment['operation_id']==read['operation_id'] and assignment['action']=='read'
    assert assignment['session_binding'] != raw and len(assignment['session_binding'])==64
    authority = rv.machine(env(),'/home/authorize',bound(assignment,2))
    assert authority['allowed'] and authority['expires']==clock[0]+2
    result = bound(assignment,2)|{'outcome':{'status':'confirmed','state':'off'}}
    assert rv.machine(env(),'/home/result',result)['accepted']
    assert rv.machine(env(),'/home/result',result)['accepted']
    assert rv.operation(raw,env(),read['operation_id'])['state']=='off'
    command = queue(rv,raw,'on')
    assignment = poll(rv,3)['operation']
    store.operator('revoke','guest@example.invalid')
    assert rv.machine(env(),'/home/authorize',bound(assignment,3))['allowed'] is False
    with pytest.raises(AccessDenied): rv.operation(raw,env(),command['operation_id'])


def test_isp_rotation_reuses_approval_but_never_old_source_or_generation(house):
    rv,store,raw,clock = house
    initial = poll(rv,1)
    old = queue(rv,raw)
    assignment = poll(rv,2)['operation']
    rv.access(raw,env(B))
    changed = poll(rv,3,B)
    assert changed['generation']==initial['generation']+1 and changed['operation'] is None
    assert store.status(raw)=='approved'
    with pytest.raises(AccessDenied): queue(rv,raw,peer=A)
    assert rv.machine(env(B),'/home/authorize',bound(assignment,2))['allowed'] is False
    new = queue(rv,raw,peer=B)
    assert new['status']=='queued'
    rv.access(raw,env(A))
    back = poll(rv,4,A)
    assert back['generation']==changed['generation']+1 and back['operation'] is None
    assert rv.machine(env(A),'/home/authorize',bound(assignment,2))['allowed'] is False
    with pytest.raises(AccessDenied): rv.operation(raw,env(A),old['operation_id'])
    with pytest.raises(AccessDenied): queue(rv,raw,peer=B)
    assert queue(rv,raw,peer=A)['status']=='queued'


def test_same_egress_freshness_gap_does_not_resurrect_preoutage_tap(house):
    rv,store,raw,clock = house
    initial = poll(rv,1)
    old = queue(rv,raw)
    clock[0]+=7
    with pytest.raises(HomeUnavailable): queue(rv,raw)
    resumed = poll(rv,2)
    assert resumed['generation']==initial['generation']+1 and resumed['operation'] is None
    with pytest.raises(AccessDenied): rv.operation(raw,env(),old['operation_id'])
    assert queue(rv,raw)['status']=='queued'


def test_stale_poll_cannot_restore_old_source_or_refresh_authority(house):
    rv,store,raw,clock = house
    body = {'agent_id':AGENT,'sequence':1,'request_id':str(uuid.uuid4()),'light':LIGHT}
    first = rv.machine(env(),'/home/poll',body)
    assert rv.machine(env(),'/home/poll',body)==first
    poll(rv,2,B)
    with pytest.raises(OperationConflict): rv.machine(env(),'/home/poll',body)
    with pytest.raises(OperationConflict): poll(rv,2,A)
    clock[0]+=7
    with pytest.raises(OperationConflict): poll(rv,2,B)
    assert store.status(raw)=='approved'


@pytest.mark.parametrize('extra', [{'boomer.direct_peer':False},{'HTTP_X_FORWARDED_FOR':A},{'HTTP_FORWARDED':'for=home'},{'HTTP_X_REAL_IP':A},{'wsgi.url_scheme':'http'},{'REMOTE_ADDR':str(ipaddress.IPv6Address(1))}])
def test_unsupported_proxy_tls_and_ipv6_fail_closed(house,extra):
    rv,store,raw,clock = house
    with pytest.raises(AccessDenied): rv.machine(env(**extra),'/home/poll',{'agent_id':AGENT,'sequence':1,'request_id':str(uuid.uuid4()),'light':LIGHT})
    assert rv.access(raw,env(**extra)) is False


def test_cellular_forged_credentials_unknown_session_and_offline_do_not_enqueue(house):
    rv,store,raw,clock = house
    poll(rv,1)
    with pytest.raises(AccessDenied): queue(rv,raw,peer=CELL)
    with pytest.raises(AccessDenied): queue(rv,'unknown')
    assert rv.access(raw,env(CELL)) is False
    with pytest.raises(AccessDenied): rv.machine(env(HTTP_AUTHORIZATION='Bearer wrong'),'/home/poll',{'agent_id':AGENT,'sequence':2,'request_id':str(uuid.uuid4()),'light':LIGHT})
    clock[0]+=7
    with pytest.raises(HomeUnavailable): queue(rv,raw)
    with store._transaction() as db: assert db.execute('SELECT COUNT(*) FROM home_operations').fetchone()[0]==0


def test_authority_expires_and_truth_result_survives_later_address_change(house):
    rv,store,raw,clock = house
    poll(rv,1)
    item=queue(rv,raw)
    assignment=poll(rv,2)['operation']
    assert rv.machine(env(),'/home/authorize',bound(assignment,2))['allowed']
    rv.access(raw,env(B))
    poll(rv,3,B)
    result=bound(assignment,2)|{'outcome':{'status':'confirmed','state':'on'}}
    assert rv.machine(env(B),'/home/result',result)['accepted']
    with pytest.raises(OperationConflict): rv.machine(env(B),'/home/result',result|{'outcome':{'status':'unknown','state':'unavailable'}})
    with pytest.raises(AccessDenied): rv.operation(raw,env(B),item['operation_id'])
    fresh=queue(rv,raw,peer=B)
    assignment=poll(rv,4,B)['operation']
    clock[0]+=9
    assert rv.machine(env(B),'/home/authorize',bound(assignment,4))['allowed'] is False
    assert poll(rv,5,B)['operation'] is None


def test_demand_only_changes_cadence_not_authority(house):
    rv,store,raw,clock=house
    with store._transaction() as db: db.execute('DELETE FROM home_demand')
    assert rv.access(raw,env(B)) is False
    first=poll(rv,1,A)
    assert first['poll_after']==15
    with pytest.raises(AccessDenied): queue(rv,raw,peer=B)
    changed=poll(rv,2,B)
    assert changed['poll_after']==1 and changed['operation'] is None
    clock[0]+=11
    assert poll(rv,3,B)['poll_after']==15
    with pytest.raises(ValueError): HomeRendezvous(store,KEY,idle_poll_seconds=14)


def test_single_assignment_no_late_redelivery_and_no_expiry_extension(house):
    rv,store,raw,clock=house
    poll(rv,1)
    op_id=str(uuid.uuid4())
    original=queue(rv,raw,operation_id=op_id)
    assignment=poll(rv,2)['operation']
    assert assignment['operation_id']==op_id
    clock[0]+=4
    assert poll(rv,3)['operation'] is None
    assert rv.machine(env(),'/home/authorize',bound(assignment,2))['allowed'] is False
    clock[0]+=4
    assert poll(rv,4)['operation'] is None
    retried=queue(rv,raw,operation_id=op_id)
    assert retried['expires']==original['expires'] and retried['status']=='not_started'
    assert rv.machine(env(),'/home/authorize',bound(assignment,2))['allowed'] is False


def test_agent_identity_and_wrong_assignment_generation_never_take_over(house):
    rv,store,raw,clock=house
    poll(rv,1)
    queue(rv,raw)
    assignment=poll(rv,2)['operation']
    with pytest.raises(OperationConflict): poll(rv,3,agent_id=str(uuid.uuid4()))
    with pytest.raises(OperationConflict): rv.machine(env(),'/home/authorize',bound(assignment,2)|{'generation':assignment['generation']+1})
    with pytest.raises(OperationConflict): rv.machine(env(),'/home/authorize',bound(assignment,3))
    clock[0]-=1
    with pytest.raises(HomeUnavailable): queue(rv,raw)
    with pytest.raises(HomeUnavailable): poll(rv,3)
    assert rv.machine(env(),'/home/authorize',bound(assignment,2))['allowed'] is False


def test_grant_expiry_never_outlives_source_proof(house):
    rv,store,raw,clock=house
    poll(rv,1)
    queue(rv,raw)
    assignment=poll(rv,2)['operation']
    clock[0]+=5.9
    authority=rv.machine(env(),'/home/authorize',bound(assignment,2))
    assert authority['allowed'] and authority['expires']==1006.0


@pytest.mark.parametrize('route', ['authorize','result','missing_result','unknown_peer'])
def test_authenticated_nonpoll_source_observation_retires_old_epoch(house,route):
    rv,store,raw,clock=house
    poll(rv,1)
    queue(rv,raw)
    assignment=poll(rv,2)['operation']
    assert rv.machine(env(),'/home/authorize',bound(assignment,2))['allowed']
    body=bound(assignment,2)
    if route=='authorize':
        assert rv.machine(env(B),'/home/authorize',body)['allowed'] is False
    elif route=='unknown_peer':
        with pytest.raises(AccessDenied): rv.machine(env(HTTP_FORWARDED='for=unknown'),'/home/authorize',body)
    else:
        body['outcome']={'status':'confirmed','state':'off'}
        if route=='missing_result':
            body['operation_id']=str(uuid.uuid4())
            with pytest.raises(OperationConflict): rv.machine(env(B),'/home/result',body)
        else:
            assert rv.machine(env(B),'/home/result',body)['accepted']
    assert rv.machine(env(A),'/home/authorize',bound(assignment,2))['allowed'] is False
    assert rv.access(raw,env(A)) is False


@pytest.mark.parametrize('leased', [False,True])
def test_observed_guest_a_b_a_cannot_reactivate_old_tap(house,leased):
    rv,store,raw,clock=house
    poll(rv,1)
    item=queue(rv,raw)
    assignment=poll(rv,2)['operation'] if leased else None
    assert rv.access(raw,env(B)) is False
    assert rv.access(raw,env(A)) is True
    status=rv.operation(raw,env(A),item['operation_id'])
    assert status['status']=='not_started' and status['state']=='unavailable'
    if assignment:
        assert rv.machine(env(A),'/home/authorize',bound(assignment,2))['allowed'] is False
    else:
        assert poll(rv,2)['operation'] is None


def test_late_receipt_is_truth_but_never_fresh_state_or_new_authority(house):
    rv,store,raw,clock=house
    poll(rv,1)
    item=queue(rv,raw)
    assignment=poll(rv,2)['operation']
    assert rv.machine(env(),'/home/authorize',bound(assignment,2))['allowed']
    clock[0]+=4
    poll(rv,3)
    clock[0]+=4
    poll(rv,4)
    result=bound(assignment,2)|{'outcome':{'status':'confirmed','state':'on'}}
    assert rv.machine(env(),'/home/result',result)['accepted']
    projected=rv.operation(raw,env(),item['operation_id'])
    assert projected['status']=='unknown' and projected['state']=='unavailable'
    with pytest.raises(HomeUnavailable): queue(rv,raw,'off')


def test_read_compaction_preserves_mutation_dedupe_and_clears_terminal_private_facts(house):
    rv,store,raw,clock=house
    poll(rv,1)
    read=queue(rv,raw)
    assignment=poll(rv,2)['operation']
    rv.machine(env(),'/home/authorize',bound(assignment,2))
    rv.machine(env(),'/home/result',bound(assignment,2)|{'outcome':{'status':'confirmed','state':'off'}})
    mutation=queue(rv,raw,'on')
    assignment=poll(rv,3)['operation']
    rv.machine(env(),'/home/authorize',bound(assignment,3))
    result=bound(assignment,3)|{'outcome':{'status':'confirmed','state':'on'}}
    rv.machine(env(),'/home/result',result)
    clock[0]+=70
    rv.access(raw,env())
    poll(rv,4)
    with store._transaction() as db:
        assert db.execute('SELECT 1 FROM home_operations WHERE id=?',(read['operation_id'],)).fetchone() is None
        retained=db.execute('SELECT * FROM home_operations WHERE id=?',(mutation['operation_id'],)).fetchone()
        assert retained['session']=='' and retained['peer']=='' and retained['outcome']
    assert rv.machine(env(),'/home/result',result)['accepted']
    with pytest.raises(OperationConflict): queue(rv,raw,'on',operation_id=mutation['operation_id'])


def test_first_visit_waits_for_demand_aware_poll_before_ready_or_enqueue(house):
    rv,store,raw,clock=house
    with store._transaction() as db: db.execute('DELETE FROM home_demand')
    assert poll(rv,1)['poll_after']==15
    assert rv.access(raw,env()) is False
    with pytest.raises(HomeUnavailable): queue(rv,raw)
    clock[0]+=1
    assert poll(rv,2)['poll_after']==1
    assert rv.access(raw,env()) is True
    assert queue(rv,raw)['status']=='queued'
    assert rv.display(raw,env())==LIGHT
    with pytest.raises(AccessDenied): rv.display(raw,env(CELL))


def test_multicast_is_not_accepted_as_direct_tcp_peer(house):
    rv,store,raw,clock=house
    multicast=str(ipaddress.IPv4Address(3758096385))
    with pytest.raises(AccessDenied): poll(rv,1,multicast)


def test_exact_poll_retry_from_another_source_retires_epoch_but_lower_poll_cannot(house):
    rv,store,raw,clock=house
    poll(rv,1)
    queue(rv,raw)
    body={'agent_id':AGENT,'sequence':2,'request_id':str(uuid.uuid4()),'light':LIGHT}
    assignment=rv.machine(env(A),'/home/poll',body)['operation']
    with pytest.raises(OperationConflict): rv.machine(env(B),'/home/poll',body|{'sequence':1})
    assert rv.machine(env(A),'/home/authorize',bound(assignment,2))['allowed']
    with pytest.raises(OperationConflict): rv.machine(env(B),'/home/poll',body)
    assert rv.machine(env(A),'/home/authorize',bound(assignment,2))['allowed'] is False
