import copy
import importlib.util
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent

def load(name, filename):
    spec=importlib.util.spec_from_file_location(name,ROOT/filename)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod

h=load('hardware','hardware-profile.py')
m=load('manifest2','image-manifest.py')
e=load('export2','export-module2.py')

class Module2(unittest.TestCase):
    def setUp(self):
        self.data=json.loads((ROOT/'module2-images.example.json').read_text())
        for x in self.data.values():x['id']='A'*20

    def test_fw_preserves_disk_nics_and_legacy_efi(self):
        args=h.arguments('br_fw',self.data['br_fw']['hardware'],'pool:vm-206-disk-0',301,'vmbr0','pool:vm-206-disk-1')
        self.assertIn('--ide0',args)
        self.assertIn('e1000=BC:24:11:C3:F0:C0,bridge=vmbr306,firewall=0',args)
        self.assertIn('e1000=BC:24:11:04:C1:6F,bridge=vmbr307,firewall=0',args)
        self.assertIn('pool:vm-206-disk-1,efitype=2m,pre-enrolled-keys=0',args)

    def test_unknown_network_and_shell_fields_rejected(self):
        for field,value in [('machine','$(touch /tmp/evil)'),('bios',None),('uuid',None),('disk_slot','ide0\n--args'),('uuid','bad')]:
            hw=copy.deepcopy(self.data['isp']['hardware']);hw[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):h.validate('isp',hw)
        hw=copy.deepcopy(self.data['br_fw']['hardware']);hw['nics'][0]['link']=1
        with self.assertRaises(ValueError):h.validate('br_fw',hw)

    def test_efi_required_and_module2_size_calculation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'manifest';p.write_text(json.dumps(self.data));self.assertEqual(len(m.load_manifest(p)),9)
            self.data.pop('br_fw_efi');p.write_text(json.dumps(self.data))
            with self.assertRaisesRegex(ValueError,'EFI'):m.load_manifest(p)
            verified={k:{'virtual_size':6*1024**3,'allocated_bytes':1024**3} for k in m.MODULE2_ROLES}
            p.write_text(json.dumps(verified))
            self.assertEqual(m.capacity(p,'dir')['virtual_bytes'],48*1024**3)

    def export(self,failed=False,running=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'out';calls=[]
            ids=list(range(200,208));cfgs={}
            for role,vmid in zip(h.LINKS,ids):
                hw=self.data[role]['hardware']
                lines=['bios: '+hw['bios'],'machine: '+hw['machine'],'scsihw: '+hw['scsihw'],'smbios1: uuid='+hw['uuid'],hw['disk_slot']+': local-lvm:vm-'+str(vmid)+'-disk-0']
                if role=='br_fw':lines.append('efidisk0: local-lvm:vm-206-disk-1,efitype=2m')
                for i,n in enumerate(hw['nics']):lines.append(f"net{i}: {n['model']}={n['mac']},bridge=vmbr{0 if n['link']=='uplink' else n['link']}")
                cfgs[str(vmid)]='\n'.join(lines)
            locked=set()
            def run(*args):
                calls.append(args)
                if args[:2]==('qm','status'):return 'status: running' if running else 'status: stopped'
                if args[:2]==('qm','config'):return cfgs[args[2]]+ ('\nlock: backup' if args[2] in locked else '')
                if args[:2]==('qemu-img','info'):return json.dumps({'virtual-size':32*1024**3})
                if args[:2]==('pvesm','path'):return '/fake/'+args[2].split(':')[-1]
                raise AssertionError(args)
            def subprocess_run(args,**kw):
                calls.append(tuple(args))
                if args[:2]==['qm','set']:locked.add(args[2])
                if args[:2]==['qm','unlock']:locked.remove(args[2])
                return types.SimpleNamespace(returncode=0)
            def popen(args):
                calls.append(tuple(args));Path(args[-1]).write_bytes(b'disk')
                return types.SimpleNamespace(poll=lambda:1 if failed else 0,returncode=1 if failed else 0)
            with patch.object(e,'run',side_effect=run),patch.object(e.os,'geteuid',return_value=0),patch('builtins.open',return_value=io.StringIO()),patch.object(e.fcntl,'flock'),patch.object(e.subprocess,'run',side_effect=subprocess_run),patch.object(e.subprocess,'Popen',side_effect=popen),patch('sys.argv',['export','--ids',','.join(map(str,ids)),'--output',str(root)]):
                if failed or running:
                    with self.assertRaises(ValueError):e.main()
                else:
                    e.main();manifest=json.loads((root/'module2-images.pending.json').read_text())
                    self.assertEqual(len(manifest),9)
                    self.assertTrue((root/'READY.txt').exists())
                    self.assertEqual(manifest['br_fw']['hardware']['disk_slot'],'ide0')
                    self.assertTrue(all(x['sha256'] and x['bytes']==4 for x in manifest.values()))
            self.assertFalse(locked)
            if running:self.assertFalse(root.exists());self.assertFalse(any(x[:2]==('qm','set') for x in calls))
            elif failed:self.assertFalse((root/'READY.txt').exists())
            else:self.assertEqual(sum(x[:2]==('qemu-img','compare') for x in calls),9)

    def test_export_success_checks_all_disks_and_unlocks(self):self.export()
    def test_failed_export_unlocks_and_is_not_marked_ready(self):self.export(failed=True)
    def test_running_source_refused_before_changes(self):self.export(running=True)

if __name__=='__main__':unittest.main()
