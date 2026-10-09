import importlib.util
import json
import socket
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
if not (ROOT/'deploy.sh').exists(): ROOT = Path('outputs/proxmox-adapters').resolve()
spec = importlib.util.spec_from_file_location('manager', ROOT / 'lab-manager.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class ManagerChecks(unittest.TestCase):
 def setUp(self):
  self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.root = Path(self.tmp.name); self.journal = self.root/'demo-deploy.ABCDEF12'; self.journal.mkdir()
  self.interfaces=self.root/'interfaces'; self.interfaces.write_text('auto lo\n')
  self.configs=self.root/'qemu-server';self.configs.mkdir();self.cts=self.root/'lxc';self.cts.mkdir()
  for target,value in [('HOME',self.root),('NET',self.root/'net'),('INTERFACES',self.interfaces),('VM_CONFIG_DIRS',(self.configs,self.cts))]:
   p=patch.object(m,target,value);p.start();self.addCleanup(p.stop)
  self.args=types.SimpleNamespace(journal=str(self.journal),image_dir=str(self.root/'cache'),profile='base')
  m.initialize(self.args)
  self.vm=dict(id=200,name='ISP',uuid='2f54b71c-9905-45ca-a405-52b775fc60de',volumes={'scsi0':'local-lvm:vm-200-disk-0'},networks={'net0':'virtio,bridge=vmbr0,firewall=0'},memory=1024,cores=1)
  self.cfg={'smbios1':'uuid='+self.vm['uuid'],'name':'ISP','description':m.marker(self.journal),'scsi0':'local-lvm:vm-200-disk-0,size=32G','net0':self.vm['networks']['net0'],'memory':'1024','cores':'1'}
  self.block='\n# proxmox-demo-lab 200\nauto vmbr201\niface vmbr201 inet manual\n    bridge-ports none\n    bridge-stp off\n    bridge-fd 0\n'
  self.interfaces.write_text('auto lo\n'+self.block)
  p,d=m.load(self.args.journal);d['vms']=[self.vm];d['bridges']=[{'name':'vmbr201','block':self.block}];m.save(p/'lab.json',d)
 def resources(self):return {200:{'vmid':200,'type':'qemu','node':socket.gethostname().split('.')[0]}}
 def call(self,*args):
  if args[0]=='qm' and args[1]=='status':return 'status: stopped'
  raise AssertionError(args)
 def test_identity_and_disk_changes_rejected(self):
  for change in [{'name':'somebody-else'},{'smbios1':'uuid=183dc834-cc94-4627-818f-7157bded41e4'},{'scsi0':'local-lvm:vm-100-disk-0'},{'net0':'virtio,bridge=vmbr7'},{'description':'another deployment'}]:
   with self.subTest(change=change),self.assertRaises(ValueError):m.verify_vm(self.journal,self.vm,{**self.cfg,**change})
 def test_delete_cancel_makes_no_changes(self):
  with patch.object(m,'existing_resources',side_effect=self.resources),patch.object(m,'config',return_value=self.cfg),patch.object(m,'run',side_effect=self.call),patch.object(m,'confirm',return_value=False),patch.object(m.subprocess,'run') as mutation:
   m.delete_lab(self.args);mutation.assert_not_called()
  self.assertIn(self.block,self.interfaces.read_text());self.assertFalse(m.load(self.args.journal)[1]['deleted'])
 def test_running_vm_refuses_before_confirmation(self):
  with patch.object(m,'existing_resources',side_effect=self.resources),patch.object(m,'config',return_value=self.cfg),patch.object(m,'run',return_value='status: running'),patch.object(m,'confirm') as confirmation,patch.object(m.subprocess,'run') as mutation:
   with self.assertRaises(ValueError):m.delete_lab(self.args)
   mutation.assert_not_called();confirmation.assert_not_called()
 def test_foreign_bridge_reference_refuses(self):
  (self.cts/'999.conf').write_text('net0: name=eth0,bridge=vmbr201\n')
  with self.assertRaisesRegex(ValueError,'another VM'):m.bridge_removal_plan(m.load(self.args.journal)[1],{200})
 def test_live_host_address_refuses_bridge_deletion(self):
  (m.NET/'vmbr201').mkdir(parents=True)
  with patch.object(m,'run',return_value='[{"addr_info":[{"family":"inet","scope":"global","local":"192.168.0.105"}]}]'):
   with self.assertRaisesRegex(ValueError,'host IP'):m.bridge_removal_plan(m.load(self.args.journal)[1],{200})
 def test_live_foreign_port_refuses_bridge_deletion(self):
  ports=m.NET/'vmbr201'/'brif';ports.mkdir(parents=True);(ports/'eno1').touch()
  with self.assertRaisesRegex(ValueError,'live ports'):m.bridge_removal_plan(m.load(self.args.journal)[1],{200})
 def test_shared_disk_added_during_confirmation_refuses(self):
  def confirmation(text):
   (self.configs/'999.conf').write_text('scsi0: local-lvm:vm-200-disk-0\n');return True
  with patch.object(m,'existing_resources',side_effect=self.resources),patch.object(m,'config',return_value=self.cfg),patch.object(m,'run',side_effect=self.call),patch.object(m,'confirm',side_effect=confirmation),patch.object(m.subprocess,'run') as mutation:
   with self.assertRaisesRegex(ValueError,'another VM'):m.delete_lab(self.args)
   mutation.assert_not_called()
 def test_shared_disk_refuses_deletion(self):
  (self.configs/'999.conf').write_text('scsi0: local-lvm:vm-200-disk-0\n')
  with patch.object(m,'existing_resources',side_effect=self.resources),patch.object(m,'config',return_value=self.cfg),patch.object(m,'run',side_effect=self.call),patch.object(m.subprocess,'run') as mutation:
   with self.assertRaisesRegex(ValueError,'another VM'):m.delete_lab(self.args)
   mutation.assert_not_called()
 def test_changed_bridge_config_refuses(self):
  self.interfaces.write_text(self.interfaces.read_text().replace('bridge-ports none','bridge-ports eno1'))
  with self.assertRaisesRegex(ValueError,'changed'):m.bridge_removal_plan(m.load(self.args.journal)[1],{200})
 def test_successful_delete_targets_only_owned_resources(self):
  with patch.object(m,'existing_resources',side_effect=[self.resources(),{}]),patch.object(m,'config',return_value=self.cfg),patch.object(m,'run',side_effect=self.call),patch.object(m,'confirm',return_value=True),patch.object(m.subprocess,'run') as mutation:
   m.delete_lab(self.args)
   self.assertEqual([x.args[0] for x in mutation.call_args_list],[['qm','destroy','200']])
  self.assertEqual(self.interfaces.read_text(),'auto lo\n');self.assertTrue(m.load(self.args.journal)[1]['deleted']);self.assertTrue(list(self.journal.glob('interfaces.before-delete-*')))
 def test_failed_destroy_retains_bridges(self):
  with patch.object(m,'existing_resources',side_effect=self.resources),patch.object(m,'config',return_value=self.cfg),patch.object(m,'run',side_effect=self.call),patch.object(m,'confirm',return_value=True),patch.object(m.subprocess,'run',side_effect=m.subprocess.CalledProcessError(1,['qm','destroy','200'])):
   with self.assertRaises(m.subprocess.CalledProcessError):m.delete_lab(self.args)
  self.assertIn(self.block,self.interfaces.read_text())
 def test_reused_id_with_foreign_type_refuses(self):
  resources=self.resources();resources[200]['type']='lxc'
  with patch.object(m,'existing_resources',return_value=resources),patch.object(m.subprocess,'run') as mutation:
   with self.assertRaisesRegex(ValueError,'another'):m.delete_lab(self.args)
   mutation.assert_not_called()
 def make_complete(self):
  p,d=m.load(self.args.journal);d['complete']=True;d['vms']=[{**self.vm,'id':200+i} for i in range(8)];m.save(p/'lab.json',d)
 def test_snapshot_unsupported_refuses_all(self):
  self.make_complete()
  def api(*args):return json.dumps([] if args[2].endswith('/snapshot') else {'hasFeature':0})
  with patch.object(m,'safe_stopped'),patch.object(m,'run',side_effect=api),patch.object(m.subprocess,'run') as mutation:
   with self.assertRaisesRegex(ValueError,'do not support'):m.snapshots(self.args)
   mutation.assert_not_called()
 def test_snapshots_are_disk_only_and_not_overwritten(self):
  self.make_complete()
  def api(*args):
   if args[2].endswith('/snapshot'):return json.dumps([{'name':'lab-initial'}] if '/200/' in args[2] else [])
   return '{"hasFeature":1}'
  with patch.object(m,'safe_stopped'),patch.object(m,'run',side_effect=api),patch.object(m.subprocess,'run') as mutation:
   m.snapshots(self.args)
   self.assertEqual(len(mutation.call_args_list),7)
   for c in mutation.call_args_list:
    a=c.args[0];self.assertEqual(a[:2],['qm','snapshot']);self.assertEqual(a[a.index('--vmstate')+1],'0');self.assertNotEqual(a[2],'200')
 def test_cache_cleanup_keeps_unknown_files(self):
  cache=Path(self.args.image_dir);cache.mkdir();(cache/'alt-jeos.qcow2').write_bytes(b'qcow2');(cache/'alt-jeos.qcow2.demo.json').write_text('{}');(cache/'personal.qcow2').write_bytes(b'keep')
  with patch.object(m,'confirm',return_value=True):m.clean_cache(self.args)
  self.assertEqual([p.name for p in cache.iterdir()],['personal.qcow2'])
 def test_cache_symlink_refused_without_deleting_target(self):
  cache=Path(self.args.image_dir);cache.mkdir();target=self.root/'important';target.write_text('keep');(cache/'alt-jeos.qcow2').symlink_to(target)
  with self.assertRaisesRegex(ValueError,'Symlink'):m.clean_cache(self.args)
  self.assertEqual(target.read_text(),'keep')
 def test_legacy_registration_checks_provenance_and_changes_no_vms(self):
  (self.journal/'lab.json').unlink();(self.journal/'created.tsv').write_text(''.join(str(200+i)+'\t'+role+'\n' for i,role in enumerate(m.ROLES)))
  (self.journal/'interfaces.before').write_text('auto lo\n')
  text='auto lo\n'
  for i in range(201,210):text+=self.block.replace('vmbr201','vmbr'+str(i))
  self.interfaces.write_text(text)
  def cfg(i):
   return {**self.cfg, 'name':'TEST-'+m.ROLES[i-200], 'scsi0':f'local-lvm:vm-{i}-disk-0',
      'description':'proxmox-demo-lab: imported OS and hardware; journal '+str(self.journal),
      'net0': 'virtio,bridge=vmbr'+str(201+i-200)+',firewall=0',
      **({'net1':'virtio,bridge=vmbr209,firewall=0'} if i==200 else {})}
  resources={i:{'vmid':i,'type':'qemu','node':socket.gethostname().split('.')[0]} for i in range(200,208)}
  with patch.object(m,'existing_resources',return_value=resources),patch.object(m,'config',side_effect=cfg),patch.object(m,'confirm',return_value=True),patch.object(m.subprocess,'run') as mutation:
   m.adopt_legacy(self.args);mutation.assert_not_called()
  p,d=m.load(self.args.journal);self.assertTrue(d['complete']);self.assertEqual(d['profile'],'legacy');self.assertEqual(len(d['bridges']),9)
  for vm in d['vms']:m.verify_vm(p,vm,cfg(vm['id']))
 def test_cache_used_by_vm_refuses_cleanup(self):
  cache=Path(self.args.image_dir);cache.mkdir();f=cache/'alt-jeos.qcow2';f.write_bytes(b'keep')
  (self.configs/'999.conf').write_text('scsi0: '+str(f)+'\n')
  with self.assertRaisesRegex(ValueError,'uses a disk'):m.clean_cache(self.args)
  self.assertTrue(f.exists())
 def test_module_requires_separate_role_images(self):
  d=json.loads((ROOT/'module1-images.example.json').read_text());f=self.root/'module.json';f.write_text(json.dumps(d))
  with self.assertRaises(ValueError):m.images.load_manifest(f)
  for item in d.values():item['id']='A'*20
  f.write_text(json.dumps(d));self.assertEqual(set(m.images.load_manifest(f)),m.images.ROLE_KINDS)
  verified={k:{'virtual_size':32*1024**3,'allocated_bytes':2*1024**3} for k in d};f.write_text(json.dumps(verified))
  self.assertEqual(m.images.capacity(f,'lvmthin')['required_kb'],28*1024**2)

if __name__=='__main__':unittest.main()
