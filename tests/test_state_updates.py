import json
import uuid
import pytest

from boomer_switch.state_updates import StateUpdates
from test_rendezvous import house, poll, env, bound, AGENT, B
from boomer_switch.registration import AccessDenied
from boomer_switch.home_rendezvous import HomeUnavailable, OperationConflict


def prepare(house):
    rv, store, raw, clock = house
    poll(rv, 1)
    with store._transaction() as db:
        StateUpdates.install(db)
    return rv, store, raw, clock


def report(db, target, created, *, generation=1, status='confirmed', state='off', background=1):
    identity = str(uuid.uuid4())
    outcome = json.dumps({'status':status,'state':state}, sort_keys=True)
    db.execute("INSERT INTO home_operations(id,session,peer,generation,action,status,created,expires,outcome,completed,light_id,targeted,background) VALUES (?,'synthetic-session','synthetic-peer',?,'read','completed',?,?,?,?,?,1,?)",
               (identity,generation,created,created+8,outcome,created+1,target,background))
    return identity


def test_shared_read_budget_and_room_preference_do_not_starve_catalog(house):
    _, store, _, clock = prepare(house)
    catalog = [{'light_id':'sample-'+str(i)} for i in range(15)]
    selected = []
    # Two callers prefer separate rooms. Every third admission still serves
    # the oldest light in the complete catalog.
    with store._transaction() as db:
        current = db.execute('SELECT * FROM home_connection').fetchone()
        for index in range(45):
            preferred = {item['light_id'] for item in catalog[(0 if index % 2 else 5):(5 if index % 2 else 10)]}
            target = StateUpdates.target(db,current,catalog,preferred,clock[0])
            if target is not None:
                selected.append((target,clock[0]));report(db,target,clock[0])
                assert StateUpdates.target(db,current,catalog,set(),clock[0]) is None
                assert StateUpdates.target(db,current,catalog,set(),clock[0]+1.999) is None
            clock[0] += 2
    assert {target for target,_ in selected} == {item['light_id'] for item in catalog}
    assert all(after-before >= 2 for (_,before),(_,after) in zip(selected,selected[1:]))


def test_reports_keep_last_confirmation_and_separate_stale_from_failure(house):
    _, store, _, clock = prepare(house)
    catalog = [{'light_id':'selected-lamp'}]
    with store._transaction() as db:
        current = db.execute('SELECT * FROM home_connection').fetchone()
        report(db,'selected-lamp',clock[0]-50)
        stale, = StateUpdates.snapshots(db,current,catalog,clock[0])
        assert stale['state']=='off' and stale['availability']=='stale' and stale['age_ms']==50000
        report(db,'selected-lamp',clock[0]-1,status='not_started',state='unavailable')
        failed, = StateUpdates.snapshots(db,current,catalog,clock[0])
        assert failed['state']=='unavailable' and failed['availability']=='unavailable'
        assert failed['last_confirmed']=={'state':'off','age_ms':50000}
        assert set(failed)=={'light_id','state','availability','age_ms','sample','last_confirmed'}


def test_late_or_future_reports_never_become_fresh(house):
    _, store, _, clock = prepare(house)
    with store._transaction() as db:
        current = db.execute('SELECT * FROM home_connection').fetchone()
        report(db,'old',clock[0]-100)
        report(db,'future',clock[0]+1)
        values = StateUpdates.snapshots(db,current,[{'light_id':'old'},{'light_id':'future'}],clock[0])
        assert [value['availability'] for value in values]==['stale','unknown']
        assert values[0]['age_ms']==100000 and values[1]['age_ms'] is None


def test_aged_commands_do_not_resurrect_an_older_read(house):
    _, store, _, clock = prepare(house)
    with store._transaction() as db:
        current = db.execute('SELECT * FROM home_connection').fetchone()
        report(db,'selected-lamp',clock[0]-100)
        command = report(db,'selected-lamp',clock[0]-70,state='on')
        db.execute("UPDATE home_operations SET action='on',peer='',session='' WHERE id=?",(command,))
        value, = StateUpdates.snapshots(db,current,[{'light_id':'selected-lamp'}],clock[0])
        assert value['state']=='on' and value['availability']=='stale' and value['last_confirmed']=={'state':'on','age_ms':70000}
        unknown = report(db,'selected-lamp',clock[0]-65,status='unknown',state='unavailable')
        db.execute("UPDATE home_operations SET action='off' WHERE id=?",(unknown,))
        value, = StateUpdates.snapshots(db,current,[{'light_id':'selected-lamp'}],clock[0])
        assert value['availability']=='unknown' and value['last_confirmed']['state']=='on'


def test_read_retention_is_bounded_to_latest_attempt_and_confirmation(house):
    _, store, _, clock = prepare(house)
    with store._transaction() as db:
        current = db.execute('SELECT * FROM home_connection').fetchone()
        for index in range(100):
            report(db,'selected-lamp',clock[0]+index)
        latest = report(db,'selected-lamp',clock[0]+101,status='unknown',state='unavailable')
        report(db,'other-generation',clock[0]+102,generation=2)
        keep = StateUpdates.read_keepers(db,current)
        assert len(keep)==2 and latest in keep


def test_missing_command_result_masks_older_confirmation(house):
    _, store, _, clock = prepare(house)
    with store._transaction() as db:
        current = db.execute('SELECT * FROM home_connection').fetchone()
        report(db,'selected-lamp',clock[0]-5)
        command = report(db,'selected-lamp',clock[0]-2,state='on')
        db.execute("UPDATE home_operations SET action='on',status='authorized',outcome=NULL WHERE id=?",(command,))
        value, = StateUpdates.snapshots(db,current,[{'light_id':'selected-lamp'}],clock[0])
        assert value['availability']=='unknown' and value['state']=='unavailable'
        assert value['last_confirmed']['state']=='off'
        value, = StateUpdates.snapshots(db,current,[{'light_id':'selected-lamp'}],clock[0]+20)
        assert value['availability']=='unknown'


def automatic(rv, raw, preferred=(), identity=None, **source):
    return rv.enqueue(raw,env(**source),{'operation_id':identity or str(uuid.uuid4()),'action':'read','background':True,'preferred':list(preferred)})


def finish(rv, assignment, sequence, state='off'):
    request = bound(assignment,sequence)
    if 'light_id' in assignment:
        request['light_id'] = assignment['light_id']
    assert rv.machine(env(),'/home/authorize',request)['allowed']
    rv.machine(env(),'/home/result',request|{'request_id':str(uuid.uuid4()),'outcome':{'status':'confirmed','state':state}})


def test_two_approved_guests_share_cap_and_fairness_through_real_queue(house):
    rv, store, raw, clock = house
    other = store.browser(store.invitation()['token'])
    store.start(other,'Second sample','second@example.invalid')
    store.verify(other,store.mailbox('second@example.invalid')['messages'][-1]['code'])
    store.operator('approve','second@example.invalid')
    rv.access(other,env())
    catalog = [{'light_id':'sample-'+str(i),'name':'Sample '+str(i),'room':'Room '+str(i//5)} for i in range(15)]
    sequence = 0
    def fleet_poll():
        nonlocal sequence
        sequence += 1
        return rv.machine(env(),'/home/poll',{'agent_id':AGENT,'sequence':sequence,'request_id':str(uuid.uuid4()),'lights':catalog,'catalog_revision':'a'*64})
    fleet_poll();selected=[]
    for index in range(45):
        automatic(rv,raw,[x['light_id'] for x in catalog[:5]])
        automatic(rv,other,[x['light_id'] for x in catalog[5:10]])
        with store._transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM home_operations WHERE status='queued' AND invalidated=0").fetchone()[0] <= 1
        assignment = fleet_poll()['operation']
        if assignment is not None:
            selected.append((assignment['light_id'],clock[0]));finish(rv,assignment,sequence)
        assert rv.display_state(other,env())['automatic_updates'] is True
        clock[0] += 2
    assert {target for target,_ in selected}=={x['light_id'] for x in catalog}
    assert all(after-before>=2 for (_,before),(_,after) in zip(selected,selected[1:]))


def test_tap_retires_only_unleased_automatic_read_and_wins_next_lease(house):
    rv, store, raw, clock = prepare(house)
    automatic(rv,raw)
    command = rv.enqueue(raw,env(),{'operation_id':str(uuid.uuid4()),'action':'on'})
    assignment = poll(rv,2)['operation']
    assert assignment['operation_id']==command['operation_id'] and assignment['action']=='on'
    with store._transaction() as db:
        row=db.execute('SELECT * FROM home_operations WHERE background=1').fetchone()
        assert row['status']=='not_started' and row['invalidated']==1 and row['poll_sequence'] is None
    finish(rv,assignment,2,state='on')


def test_tap_waits_behind_one_leased_read_and_blocks_more_background_work(house):
    rv, store, raw, clock = prepare(house)
    automatic(rv,raw);read = poll(rv,2)['operation']
    command = rv.enqueue(raw,env(),{'operation_id':str(uuid.uuid4()),'action':'on'})
    automatic(rv,raw)
    with pytest.raises(OperationConflict):rv.enqueue(raw,env(),{'operation_id':str(uuid.uuid4()),'action':'off'})
    with store._transaction() as db:
        assert db.execute('SELECT invalidated FROM home_operations WHERE id=?',(read['operation_id'],)).fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM home_operations').fetchone()[0]==2
    finish(rv,read,2);assignment = poll(rv,3)['operation']
    assert assignment['operation_id']==command['operation_id']
    finish(rv,assignment,3,state='on')


def test_stalled_read_does_not_extend_or_replay_expired_tap(house):
    rv, store, raw, clock = prepare(house)
    automatic(rv,raw);read = poll(rv,2)['operation']
    command = rv.enqueue(raw,env(),{'operation_id':str(uuid.uuid4()),'action':'on'})
    clock[0]+=9
    assert poll(rv,3)['operation'] is None
    with pytest.raises(AccessDenied):rv.operation(raw,env(),command['operation_id'])
    assert poll(rv,4)['operation'] is None


def test_automatic_reads_and_reports_need_current_permission_home_and_freshness(house):
    rv, store, raw, clock = prepare(house)
    with pytest.raises(AccessDenied):automatic(rv,raw,peer=B)
    with pytest.raises(AccessDenied):rv.display_state(raw,env(B))
    with pytest.raises(AccessDenied):automatic(rv,raw,HTTP_X_FORWARDED_FOR='synthetic')
    with pytest.raises(AccessDenied):automatic(rv,raw,['not-selected'])
    clock[0]+=7
    with pytest.raises(HomeUnavailable):automatic(rv,raw)
    poll(rv,2);store.operator('revoke','guest@example.invalid')
    with pytest.raises(AccessDenied):automatic(rv,raw)
    with pytest.raises(AccessDenied):rv.display_state(raw,env())


def test_compaction_preserves_bounded_read_facts_and_scrubs_personal_fields(house):
    rv, store, raw, clock = prepare(house)
    with store._transaction() as db:
        for index in range(100):report(db,'selected-lamp',clock[0]-200+index)
        report(db,'selected-lamp',clock[0]-80,status='unknown',state='unavailable')
        rv._compact(db)
        rows=db.execute("SELECT * FROM home_operations WHERE action='read'").fetchall()
        assert len(rows)==2 and all(row['session']==row['peer']=='' for row in rows)
