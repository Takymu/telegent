"""Attachment placement under outages, disk limits and verified copy-before-remove."""
from dataclasses import replace
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cluster_storage import Blob, HOT_SECONDS, MIN_FREE_BYTES, Node, Replica, plan_storage


NOW = 2000000000
MIB = 1024 * 1024


def blob(name='file-a', size=1024, age=0, copies=('node-a',)):
    digest = hashlib.sha256(name.encode()).hexdigest()
    return Blob(name, size, NOW-age, digest, tuple(Replica(node, size, digest) for node in copies))


def nodes(count=2, free=1000*MIB):
    return tuple(Node('node-'+chr(97+i), free, NOW) for i in range(count))


class ClusterStorageTest(unittest.TestCase):
    def plan(self, files, servers=None, **kw):
        return plan_storage(files, nodes() if servers is None else servers, NOW, **kw)

    def test_fresh_files_target_every_enrolled_device_for_two_twenty_and_more(self):
        for count in (2, 3, 20, 24):
            servers=tuple(Node('node-'+str(i), 1000*MIB, NOW) for i in range(count))
            item=blob(copies=('node-0',))
            placement=self.plan([item],servers).placements[0]
            self.assertEqual(len(placement.desired),count)
            self.assertEqual(len(placement.transfers),count-1)
            self.assertEqual(placement.missing_copies,count-1)
            self.assertTrue(all(t.source=='node-0' and t.blocked is None for t in placement.transfers))
            self.assertEqual(placement.removable,())

    def test_exact_age_boundary_keeps_full_replication(self):
        for age,hot in ((HOT_SECONDS-1,True),(HOT_SECONDS,True),(HOT_SECONDS+1,False),(-86400,True)):
            self.assertEqual(self.plan([blob(age=age)],nodes(3)).placements[0].hot,hot)

    def test_archive_retains_two_verified_copies_by_default(self):
        item=blob(age=HOT_SECONDS+1,copies=('node-a','node-b','node-c'))
        placement=self.plan([item],nodes(3)).placements[0]
        self.assertEqual(len(placement.desired),2)
        self.assertEqual(len(placement.removable),1)
        self.assertEqual(placement.missing_copies,0)
        self.assertEqual(set(placement.available)-set(placement.removable),set(placement.desired))

    def test_never_remove_source_before_destination_is_verified(self):
        servers=(replace(nodes(3)[0],draining=True),nodes(3)[1],nodes(3)[2])
        item=blob(age=HOT_SECONDS+1)
        placement=self.plan([item],servers).placements[0]
        self.assertEqual(set(placement.desired),{'node-b','node-c'})
        self.assertEqual(placement.transfers[0].source,'node-a')
        self.assertEqual(placement.removable,())
        item=replace(item,replicas=item.replicas+(Replica('node-b',item.size,item.sha256,False),))
        self.assertEqual(self.plan([item],servers).placements[0].removable,())
        item=replace(item,replicas=item.replicas[:1]+(replace(item.replicas[1],verified=True),))
        self.assertEqual(self.plan([item],servers).placements[0].removable,())
        item=replace(item,replicas=item.replicas+(Replica('node-c',item.size,item.sha256),))
        self.assertEqual(self.plan([item],servers).placements[0].removable,('node-a',))

    def test_wrong_size_or_hash_is_not_a_replica(self):
        item=blob(age=HOT_SECONDS+1)
        for replica in (Replica('node-b',item.size+1,item.sha256),Replica('node-b',item.size,'0'*64)):
            placement=self.plan([replace(item,replicas=item.replicas+(replica,))]).placements[0]
            self.assertEqual(placement.available,('node-a',))
            self.assertEqual(placement.removable,())
            self.assertEqual(placement.missing_copies,1)

    def test_node_outage_preserves_existing_data_and_schedules_catchup(self):
        servers=(nodes()[0],replace(nodes()[1],online=False))
        item=blob(copies=('node-a','node-b'))
        placement=self.plan([item],servers).placements[0]
        self.assertEqual(placement.desired,('node-a','node-b'))
        self.assertEqual(placement.available,('node-a',))
        self.assertEqual(placement.transfers[0].blocked,'node_offline')
        self.assertEqual(placement.removable,())
        self.assertEqual(self.plan([item]).placements[0].transfers,())

    def test_lost_only_source_is_reported_without_imaginary_copies(self):
        item=blob(copies=('unreachable-node',))
        placement=self.plan([item]).placements[0]
        self.assertEqual(placement.available,())
        self.assertEqual(placement.missing_copies,2)
        self.assertTrue(all(t.source is None and t.blocked=='source_unavailable' for t in placement.transfers))
        self.assertEqual(dict(self.plan([item]).remaining_bytes)['node-a'],1000*MIB-MIN_FREE_BYTES)

    def test_stale_or_future_inventory_is_not_used_as_a_healthy_device(self):
        for timestamp in (NOW-91,NOW+1):
            placement=self.plan([blob()],(nodes()[0],replace(nodes()[1],observed_at=timestamp))).placements[0]
            self.assertEqual(placement.transfers[0].blocked,'node_offline')
            self.assertEqual(placement.missing_copies,1)

    def test_batch_capacity_respects_reserve_without_counting_future_deletions(self):
        servers=(replace(nodes()[0],draining=True),replace(nodes()[1],free_bytes=MIN_FREE_BYTES+150))
        items=(blob('file-a',size=100),blob('file-b',size=100))
        plan=self.plan(items,servers)
        self.assertEqual([p.transfers[0].blocked for p in plan.placements],[None,'disk_space'])
        self.assertEqual(dict(plan.remaining_bytes)['node-b'],50)
        self.assertTrue(all(not p.removable for p in plan.placements))

    def test_healthy_existing_copy_is_retained_even_when_its_disk_is_full(self):
        servers=tuple(replace(n,free_bytes=1) for n in nodes())
        item=blob(age=HOT_SECONDS+1,copies=('node-a','node-b'))
        placement=self.plan([item],servers).placements[0]
        self.assertEqual(placement.missing_copies,0)
        self.assertEqual(placement.transfers,())
        self.assertEqual(placement.removable,())

    def test_weighted_archive_placement_tracks_available_space(self):
        source=Node('source',0,NOW,draining=True)
        servers=(source,Node('node-a',100*MIB,NOW),Node('node-b',100*MIB,NOW),
                 Node('node-c',300*MIB,NOW),Node('node-d',300*MIB,NOW))
        items=[blob('file-'+str(i),age=HOT_SECONDS+1,copies=('source',)) for i in range(800)]
        plan=self.plan(items,servers,reserve_bytes=0)
        fraction=sum(n in ('node-c','node-d') for p in plan.placements for n in p.desired)/(2*len(items))
        self.assertTrue(.66<fraction<.75,fraction)
        self.assertTrue(all(not p.removable for p in plan.placements))

    def test_replication_prefers_separate_failure_domains(self):
        servers=(replace(nodes(3)[0],failure_domain='site-a'),
                 replace(nodes(3)[1],failure_domain='site-a'),
                 replace(nodes(3)[2],failure_domain='site-b'))
        item=blob(age=HOT_SECONDS+1,copies=('node-a','node-b'))
        placement=self.plan([item],servers).placements[0]
        self.assertIn('node-c',placement.desired)
        self.assertEqual(placement.removable,())

    def test_plan_is_deterministic_without_mutating_input_inventory(self):
        servers=nodes(3);items=(blob('file-a'),blob('file-b',age=HOT_SECONDS+1))
        before=repr((servers,items))
        self.assertEqual(self.plan(items,servers),self.plan(reversed(items),reversed(servers)))
        self.assertEqual(repr((servers,items)),before)

    def test_rebalancing_does_not_oscillate_after_copy_and_remove(self):
        servers=(Node('node-a',2*MIB,NOW),Node('node-b',4*MIB,NOW),Node('node-c',6*MIB,NOW))
        items=tuple(blob('file-'+str(i),size=1024,age=HOT_SECONDS+1,copies=('node-a','node-b')) for i in range(100))
        plan=self.plan(items,servers,reserve_bytes=0,rebalance=True)
        desired={p.blob:set(p.desired) for p in plan.placements}
        updated=[];free={n.id:n.free_bytes for n in servers}
        for item in items:
            targets=desired[item.id];previous={r.node for r in item.replicas}
            for node in previous-targets:free[node]+=item.size
            for node in targets-previous:free[node]-=item.size
            updated.append(replace(item,replicas=tuple(Replica(target,item.size,item.sha256) for target in sorted(targets))))
        again=self.plan(updated,tuple(replace(n,free_bytes=free[n.id]) for n in servers),
                        reserve_bytes=0,rebalance=True)
        self.assertEqual({p.blob:set(p.desired) for p in again.placements},desired)
        self.assertTrue(all(not p.transfers and not p.removable for p in again.placements))

    def test_no_devices_or_insufficient_devices_never_reports_full_protection(self):
        self.assertEqual(self.plan([blob()],()).placements[0].missing_copies,1)
        placement=self.plan([blob(age=HOT_SECONDS+1)],nodes(1)).placements[0]
        self.assertEqual(placement.required,2)
        self.assertEqual(placement.missing_copies,1)
        self.assertEqual(placement.removable,())

    def test_invalid_metadata_is_refused(self):
        for bad in (replace(blob(),id='../file'),replace(blob(),size=-1),replace(blob(),sha256='bad'),
                    replace(blob(),created_at=float('nan')),replace(blob(),replicas=(Replica('node-a',1,'bad'),))):
            with self.assertRaises(ValueError):self.plan([bad])
        for bad in (replace(nodes()[0],id='..'),replace(nodes()[0],free_bytes=True),
                    replace(nodes()[0],observed_at=float('inf'))):
            with self.assertRaises(ValueError):self.plan([blob()],(bad,))
        with self.assertRaises(ValueError):self.plan([blob(),blob()])
        with self.assertRaises(ValueError):self.plan([blob()],(nodes()[0],nodes()[0]))
        with self.assertRaises(ValueError):self.plan([blob()],archive_copies=1)


if __name__=='__main__':unittest.main(verbosity=2)
