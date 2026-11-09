"""Measure shared refreshes and tap contention with synthetic devices only."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from demos.command_latency import Device, Transport, environment, KEY, SITE, LIGHT
from boomer_switch.fleet_journal import FleetJournal
from boomer_switch.home_runner import HomeRunner
from boomer_switch.home_rendezvous import HomeRendezvous
from boomer_switch.lights import Light
from boomer_switch.registration import RegistrationStore


class Resolver:
    def __init__(self,device):self.device=device
    async def __call__(self,identity):
        assert identity==self.device.device_id
        await self.device.update()
        return self.device
    async def close(self):pass


def approved(store,email):
    raw=store.browser(store.invitation()['token'])
    store.start(raw,'Sample guest',email)
    store.verify(raw,store.mailbox(email)['messages'][-1]['code'])
    store.operator('approve',email)
    return raw


def automatic(rv,raw,preferred=()):
    rv.enqueue(raw,environment(),{'operation_id':str(uuid.uuid4()),'action':'read','background':True,'preferred':list(preferred)})


async def sweep(directory):
    clock=[1000.0]
    store=RegistrationStore(directory/'site',mode='production',clock=lambda:clock[0])
    guests=[approved(store,'first@example.invalid'),approved(store,'second@example.invalid')]
    rv=HomeRendezvous(store,KEY)
    for raw in guests:rv.access(raw,environment())
    lights=[Light('sample-'+str(i), 'Sample '+str(i),'Room '+str(i//5),'synthetic-device-'+str(i)) for i in range(15)]
    class VirtualDevice(Device):
        def __init__(self,identity):super().__init__(0);self.device_id=identity
        async def update(self):self.updates+=1;clock[0]+=.05;await asyncio.sleep(0)
    devices={light.light_id:VirtualDevice(light.device_id) for light in lights}
    class VirtualTransport(Transport):
        def __init__(self):super().__init__(rv,0);self.clock=lambda:clock[0];self.leases=[]
        async def post(self,route,body):
            self.calls[route]+=1;clock[0]+=.05
            result=rv.machine(environment(),'/home/'+route,body)
            if route=='poll' and result['operation']:
                self.leases.append((clock[0],result['operation']['light_id'],result['operation']['operation_id']))
            return result
    transport=VirtualTransport()
    journal=FleetJournal(directory/'journal.sqlite',lights,SITE,clock=lambda:clock[0])
    home=HomeRunner(lights,{key:Resolver(value) for key,value in devices.items()},transport,journal,clock=lambda:clock[0])
    started=clock[0];await home.cycle();next_home=clock[0]+1;next_guest=clock[0];requests=0;first_seen={};changed=None;observed=None;max_age=0
    while clock[0]-started<90:
        if changed is None and clock[0]-started>=20:
            devices[lights[0].light_id].is_on=True;changed=clock[0]
        if clock[0]>=next_guest:
            for index,raw in enumerate(guests):
                automatic(rv,raw,[light.light_id for light in lights[index*5:(index+1)*5]])
                requests+=1
                for value in rv.display_state(raw,environment())['states']:
                    if value['sample'] is not None:first_seen.setdefault(value['light_id'],clock[0])
                    if value['age_ms'] is not None:max_age=max(max_age,value['age_ms'])
                    if changed is not None and value['light_id']==lights[0].light_id and value['state']=='on' and observed is None:observed=clock[0]
            guests.reverse();next_guest=clock[0]+2
        if clock[0]>=next_home:
            await home.cycle();next_home=clock[0]+1
        clock[0]+=.1
    assert len(first_seen)==15 and observed is not None
    assert len({row[2] for row in transport.leases})==len(transport.leases), 'A read assignment is never replayed.'
    with store._transaction() as db:
        admissions=[row[0] for row in db.execute('SELECT created FROM home_operations WHERE background=1 ORDER BY created')]
        assert all(after-before>=2 for before,after in zip(admissions,admissions[1:]))
    return {'clock':'simulated','seconds':round(clock[0]-started,2),'guests':2,'lights':15,'guest_requests':requests,
            'read_operations':len(transport.leases),'device_update_calls':sum(device.updates for device in devices.values()),
            'first_full_pass_seconds':round(max(first_seen.values())-started,2),'external_change_seconds':round(observed-changed,2),
            'largest_report_age_ms':max_age,'home_calls':dict(transport.calls),'all_targets_seen':True}


async def contention(directory,occupancy):
    store=RegistrationStore(directory/'site',mode='production');raw=approved(store,'guest@example.invalid');rv=HomeRendezvous(store,KEY);rv.access(raw,environment())
    class HeldDevice(Device):
        def __init__(self):super().__init__(.05);self.hold=False;self.entered=asyncio.Event();self.release=asyncio.Event()
        async def update(self):
            if self.hold:
                self.hold=False;self.entered.set();await asyncio.wait_for(self.release.wait(),2)
            await super().update()
    device=HeldDevice();transport=Transport(rv,.05);journal=FleetJournal(directory/'journal.sqlite',[LIGHT],SITE,clock=time.time)
    home=HomeRunner([LIGHT],{LIGHT.light_id:Resolver(device)},transport,journal)
    await home.cycle();background=None
    try:
        if occupancy!='idle':automatic(rv,raw)
        if occupancy=='leased':
            device.hold=True;background=asyncio.create_task(home.cycle());await asyncio.wait_for(device.entered.wait(),2)
        tap=time.monotonic();identity=str(uuid.uuid4())
        rv.enqueue(raw,environment(),{'operation_id':identity,'action':'on','light_id':LIGHT.light_id})
        if background:
            await asyncio.sleep(.25);device.release.set();await background
        await asyncio.sleep(.25);await home.cycle()
        result=rv.operation(raw,environment(),identity)
        assert result['status']=='confirmed' and result['state']=='on' and device.actuated is not None
        with store._transaction() as db:
            commands=db.execute("SELECT COUNT(*) FROM home_operations WHERE action='on'").fetchone()[0]
            assert commands==1
        return {'occupancy':occupancy,'clock':'wall','tap_to_actuation_ms':round((device.actuated-tap)*1000,2),
                'tap_to_confirmation_ms':round((time.monotonic()-tap)*1000,2),'command_operations':commands,'home_calls':dict(transport.calls)}
    finally:
        device.release.set()
        if background and not background.done():background.cancel();await asyncio.gather(background,return_exceptions=True)


async def run(output):
    scratch=ROOT/'.test-runs';scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='automatic-',dir=scratch) as folder:
        root=Path(folder);results={}
        results['sweep']=await sweep(root/'sweep')
        results['contention']=[]
        for state in ('idle','queued','leased'):
            results['contention'].append(await contention(root/state,state))
    results['conditions']={'http_ms':50,'device_update_ms':50,'automatic_min_ms':2000,'leased_read_hold_ms':250,'production_claim':False}
    results['home_runner_unchanged_sha256']=hashlib.sha256((ROOT/'boomer_switch/home_runner.py').read_bytes()).hexdigest()
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(results,indent=2));print(json.dumps(results))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'data/state-probe/measurement.json');options=parser.parse_args()
    asyncio.run(asyncio.wait_for(run(options.output),45))
