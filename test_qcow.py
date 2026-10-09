import json,os,subprocess,tempfile,unittest,importlib.util,types,hashlib
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parent
if not (ROOT/'deploy.sh').exists():ROOT=Path('outputs/proxmox-adapters').resolve()
spec=importlib.util.spec_from_file_location('manifest',ROOT/'image-manifest.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
spec_u=importlib.util.spec_from_file_location('uplink',ROOT/'detect-uplink.py');u=importlib.util.module_from_spec(spec_u);spec_u.loader.exec_module(u)
spec_r=importlib.util.spec_from_file_location('ranges',ROOT/'select-ranges.py');ranges=importlib.util.module_from_spec(spec_r);spec_r.loader.exec_module(ranges)
class Checks(unittest.TestCase):
 def test_uplink_nonzero_bridge_and_metric(self):
  self.assertEqual(u.select_bridge([{'dst':'default','dev':'vmbr0','metric':100}, {'dst':'default','dev':'vmbr7','metric':10}]),'vmbr7')
 def test_uplink_empty_or_nonbridge_rejected(self):
  for routes in ([], [{'dst':'default','dev':'eno1'}], [{'dst':'default','dev':'vmbr0.100'}], [{'dst':'default','dev':'vmbr7','flags':['linkdown']}], [{'dst':'default','type':'blackhole'}]):
   with self.subTest(routes=routes), self.assertRaises(ValueError):u.select_bridge(routes)
 def test_uplink_ambiguous_rejected(self):
  for routes in ([{'dst':'default','dev':'vmbr0'}, {'dst':'default','dev':'vmbr7'}], [{'dst':'default','nexthops':[{'dev':'vmbr0'}, {'dev':'vmbr7'}]}]):
   with self.subTest(routes=routes), self.assertRaises(ValueError):u.select_bridge(routes)
 def test_missing_upload_rejected(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'images.json';d={k:{'id':'A'*20,'file':k+('.txt' if k=='ideco_config' else '.qcow2')} for k in m.KINDS};d['router']['id']=None;p.write_text(json.dumps(d))
   with self.assertRaisesRegex(ValueError,'router'):m.load_manifest(p)
 def test_ideco_identity_parses_only_fields(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'config';p.write_text('bios: ovmf\nmachine: q35\nef id: ignore\nefidisk0: local-lvm:disk,efitype=4m\nsmbios1: uuid=7ec0f39e-4da7-4d58-841b-1d940259092e\nnet0: virtio=BC:24:11:90:45:4A,bridge=vmbr0\nname: $(touch /tmp/evil)\n')
   self.assertEqual(m.identity(p),('7ec0f39e-4da7-4d58-841b-1d940259092e','BC:24:11:90:45:4A'))
   p.write_text(p.read_text().replace('bios: ovmf','bios: seabios'))
   with self.assertRaises(ValueError):m.identity(p)
 def test_backing_image_rejected(self):
  with tempfile.TemporaryDirectory() as t:
   with patch.object(m.subprocess,'check_output',return_value=json.dumps({'format':'qcow2','backing-filename':'outside.raw','virtual-size':32*1024**3}).encode()):
    with self.assertRaisesRegex(ValueError,'standalone'):
     m.inspect({'jeos':{'file':'jeos.qcow2'}},Path(t),Path(t))
 def test_invalid_efi_rejected(self):
  with tempfile.TemporaryDirectory() as t:
   with patch.object(m.subprocess,'check_output',return_value=json.dumps({'format':'qcow2','virtual-size':32*1024**3}).encode()), patch.object(m.subprocess,'run'):
    with self.assertRaisesRegex(ValueError,'EFI'):
     m.inspect({'ideco_efi':{'file':'efi.qcow2'}},Path(t),Path(t))
 def test_compressed_extents_not_file_size(self):
  extents=[{'start':0,'length':65536,'data':True,'zero':False},
           {'start':65536,'length':65536,'data':True,'zero':False},
           {'start':2*1024**2,'length':1024**2,'data':False,'zero':True},
           {'start':3*1024**2,'length':65536,'data':True,'zero':True}]
  with patch.object(m.subprocess,'check_output',return_value=json.dumps(extents).encode()):
   self.assertEqual(m.allocated_bytes('compressed.qcow2'),1024**2)
 def test_thin_and_thick_capacity(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'verified.json';p.write_text(json.dumps({k:{'virtual_size':32*1024**3,'allocated_bytes':2*1024**3} for k in m.KINDS}))
   thin=m.capacity(p,'lvmthin');thick=m.capacity(p,'lvm')
   self.assertEqual(thin['required_kb'],28*1024**2)
   self.assertEqual(thick['required_kb'],289*1024**2)
 def test_download_failure_retains_completed_file(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);p=root/'jeos.qcow2';p.write_bytes(b'cached image')
   stamp=root/'jeos.qcow2.demo.json';stamp.write_text(json.dumps({'id':'good','sha256':m.digest(p)}))
   fake=types.SimpleNamespace(download=lambda **kw: (_ for _ in ()).throw(RuntimeError('access denied')))
   with patch.dict('sys.modules',{'gdown':fake}):
    with self.assertRaisesRegex(ValueError,'Google Drive could not download efi.qcow2'):
     m.download({'jeos':{'id':'good','file':'jeos.qcow2'},'ideco_efi':{'id':'efi','file':'efi.qcow2'}},root,root)
   self.assertEqual(p.read_bytes(),b'cached image');self.assertFalse((root/'efi.qcow2').exists())
 def test_local_copy_requires_published_hash(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);p=root/'efi.qcow2';p.write_bytes(b'local EFI')
   item={'id':'efi','file':'efi.qcow2','sha256':m.digest(p)}
   fake=types.SimpleNamespace(download=lambda **kw: self.fail('network was called'))
   with patch.dict('sys.modules',{'gdown':fake}):m.download({'ideco_efi':item},root,root)
   self.assertTrue((root/'efi.qcow2.demo.json').exists())
   p.write_bytes(b'wrong file')
   with patch.dict('sys.modules',{'gdown':fake}):
    with self.assertRaisesRegex(ValueError,'mismatch'):m.download({'ideco_efi':item},root,root)
 def run_deploy(self,occupied=False,low_space=False,uplink='vmbr0',explicit=False,invalid_route=False,automatic=False,module=False):
  with tempfile.TemporaryDirectory() as t:
   t=Path(t);bins=t/'bin';bins.mkdir();(t/'net'/uplink/'bridge').mkdir(parents=True);(t/'interfaces').write_text('auto lo\n'+('auto vmbr201\niface vmbr201 inet manual\n' if automatic else ''))
   verified={k:{'path':str(t/(k+'.qcow2')),'virtual_size':32*1024**3,'allocated_bytes':2*1024**3} for k in (m.ROLE_KINDS if module else m.KINDS)};(t/'verified.json').write_text(json.dumps(verified))
   if module:(t/'module1-images.json').write_text(json.dumps({k:{'id':'A'*20,'file':k+('.txt' if k.endswith('_config') else '.qcow2')} for k in m.ROLE_KINDS}))
   def exe(name,text):p=bins/name;p.write_text(text);p.chmod(0o755)
   exe('qm', '''#!/usr/bin/env python3
import sys,os,json,uuid
from pathlib import Path
p=Path(os.environ['STATE']);d=json.loads(p.read_text()) if p.exists() else {}
a=sys.argv[1:]
with open(os.environ['LOG'],'a') as f:f.write(json.dumps(a)+'\\n')
id=a[1]
if a[0]=='create':
 d[id]={'smbios1':'uuid='+str(uuid.uuid4())}
 for k,v in zip(a[2::2],a[3::2]):d[id][k.lstrip('-')]=v
if a[0]=='set':
 for k,v in zip(a[2::2],a[3::2]):
  d[id][k.lstrip('-')]=v
  if k in ('--scsi0','--sata0','--efidisk0'):d[id].pop('unused0',None)
if a[0]=='importdisk':d[id]['unused0']='local-lvm:vm-'+id+'-disk-'+('1' if 'ideco_efi' in a[2] or 'br_fw_efi' in a[2] else '0')
if a[0]=='config':
 for k,v in d[id].items():print(k+': '+v)
p.write_text(json.dumps(d))
''')
   exe('pvesh',f'#!/bin/sh\nprintf \'%s\\n\' \'{json.dumps([{"vmid":200}] if occupied else [])}\'\n')
   exe('pvesm',f"#!/bin/sh\nprintf 'Name Type Status Total Used Available %%\\nlocal-lvm lvmthin active 999999999 0 {20*1024**2 if low_space else 30*1024**2} 0\\n'\n")
   exe('qemu-img',"#!/bin/sh\nprintf '%s\\n' '{\"virtual-size\":10737418240}'\n")
   exe('ifup',"#!/bin/sh\nexit 0\n");exe('flock',"#!/bin/sh\nexit 0\n")
   exe('ip', '#!/bin/sh\nprintf \'%s\\n\' \''+json.dumps([{'dst':'default','dev':'eno1' if invalid_route else uplink}])+'\'\n')
   s=(ROOT/'deploy.sh').read_text().replace('[[ $EUID == 0 ]]', '[[ 1 == 1 ]]').replace('/sys/class/net',str(t/'net')).replace('/etc/network/interfaces',str(t/'interfaces')).replace('/run/lock/proxmox-demo-deploy.lock',str(t/'lock')).replace('/root/demo-deploy.',str(t/'demo-deploy.'))
   start=s.index('# Validate the manifest');end=s.index('storage_row=')
   journal=t/'demo-deploy.ABCDEF12';journal.mkdir();(journal/'verified.json').write_text((t/'verified.json').read_text())
   s=s[:start]+f'state="{journal}"\nlog="$state/deploy.log"\npython3 "$here/lab-manager.py" init --journal "$state" --profile {"module1" if module else "base"}\n'+s[end:]
   s=s.replace('python3 "$here/image-manifest.py" identity "$(image_path "$config_kind")"',"printf '%s\\n' '7ec0f39e-4da7-4d58-841b-1d940259092e' 'BC:24:11:90:45:4A'")
   (t/'image-manifest.py').write_text((ROOT/'image-manifest.py').read_text())
   (t/'detect-uplink.py').write_text((ROOT/'detect-uplink.py').read_text())
   (t/'select-ranges.py').write_text((ROOT/'select-ranges.py').read_text().replace('/sys/class/net',str(t/'net')).replace('/etc/network/interfaces',str(t/'interfaces')))
   manager=(ROOT/'lab-manager.py').read_text().replace("HOME = Path('/root')",f'HOME = Path({str(t)!r})').replace("INTERFACES = Path('/etc/network/interfaces')",f'INTERFACES = Path({str(t/"interfaces")!r})').replace('if os.geteuid() != 0:', 'if False:')
   (t/'lab-manager.py').write_text(manager)
   p=t/'deploy.sh';p.write_text(s)
   env=dict(os.environ,PATH=str(bins)+':'+os.environ['PATH'],STATE=str(t/'qm.json'),LOG=str(t/'calls'))
   r=subprocess.run(['bash',str(p),'--apply']+(['--profile','module1'] if module else [])+([] if automatic else ['--base-id','200','--bridge-base','201'])+(['--uplink',uplink] if explicit else []),env=env,text=True,capture_output=True,errors="replace")
   calls=[json.loads(x) for x in (t/'calls').read_text().splitlines()] if (t/'calls').exists() else []
   if invalid_route and not explicit:
    self.assertNotEqual(r.returncode,0);self.assertIn('Choose a bridge explicitly',r.stderr);self.assertEqual(calls,[]);self.assertEqual((t/'interfaces').read_text(),'auto lo\n')
   elif occupied and not automatic:
    self.assertNotEqual(r.returncode,0);self.assertIn('IDs already occupied',r.stderr);self.assertEqual(calls,[]);self.assertEqual((t/'interfaces').read_text(),'auto lo\n')
   elif low_space:
    self.assertNotEqual(r.returncode,0);self.assertIn('Not enough disk space',r.stderr);self.assertEqual(calls,[]);self.assertEqual((t/'interfaces').read_text(),'auto lo\n')
   else:
    self.assertEqual(r.returncode,0,r.stdout+r.stderr)
    creates=[a for a in calls if a[0]=='create'];self.assertEqual(len(creates),8)
    first=201 if automatic and occupied else 200
    self.assertEqual([a[1] for a in creates],[str(first+i) for i in range(8)])
    self.assertEqual([a[a.index('--name')+1] for a in creates],['ISP','HQ-RTR','HQ-SW','HQ-SRV','HQ-CLI','BR-RTR','BR-FW','BR-SRV'])
    if automatic:self.assertIn('bridge=vmbr202,firewall=0',json.dumps(calls))
    fw=next(a for a in creates if a[1]==str(first+6));self.assertEqual(fw[fw.index('--memory')+1],'6144');self.assertEqual(fw[fw.index('--cores')+1],'4')
    inventory=json.loads((journal/'lab.json').read_text());self.assertTrue(inventory['complete']);self.assertEqual(len(inventory['vms']),8);self.assertEqual(len(inventory['bridges']),9)
    if module:
     self.assertEqual(inventory['profile'],'module1')
     self.assertEqual({Path(a[2]).name for a in calls if a[0]=='importdisk'}, {k+'.qcow2' for k in m.ROLE_KINDS if not k.endswith('_config')})
    self.assertEqual(sum(a[0]=='importdisk' for a in calls),9)
    self.assertTrue(any('--efidisk0' in a and 'efitype=4m' in a[-1] for a in calls))
    self.assertTrue(any('--scsi0' in a and a[1]==str(first+3) for a in calls))
    self.assertFalse(any(a[0] in ('start','destroy','shutdown') for a in calls))
    self.assertEqual((t/'interfaces').read_text().count('bridge-ports none'),9)
    external=[a for a in calls if any('bridge='+uplink+',' in v for v in a)]
    self.assertEqual(len(external),1);self.assertEqual(external[0][1:3],[str(first),'--net0'])
    self.assertNotIn('gateway',(t/'interfaces').read_text());self.assertNotIn('address',(t/'interfaces').read_text())
 def test_full_import(self):self.run_deploy()
 def test_insufficient_thin_space_no_changes(self):self.run_deploy(low_space=True)
 def test_occupied_ids_no_changes(self):self.run_deploy(True)
 def test_full_import_with_nonzero_uplink(self):self.run_deploy(uplink='vmbr7')
 def test_explicit_uplink_overrides_host_route(self):self.run_deploy(uplink='vmbr7',explicit=True,invalid_route=True)
 def test_nonbridge_route_no_changes(self):self.run_deploy(invalid_route=True)
 def test_full_module_import_uses_distinct_disks(self):self.run_deploy(module=True)
 def test_missing_module_stops_before_any_host_commands(self):
  r=subprocess.run(['bash',str(ROOT/'deploy.sh'),'--plan','--profile','module1'],text=True,capture_output=True)
  self.assertNotEqual(r.returncode,0);self.assertIn('Module 1 images are not configured',r.stderr)
 def test_automatic_ranges_skip_existing(self):self.run_deploy(occupied=True,automatic=True)
 def test_range_selection_gaps_and_bounds(self):
  self.assertEqual(ranges.first_free({200,205,214,900},200,8,999999999),206)
  self.assertEqual(ranges.first_free(set(range(200,208)),200,8,999999999),208)
  self.assertEqual(ranges.first_free({100,101},200,8,999999999),200)
  with self.assertRaises(ValueError):ranges.first_free({9999},9991,9,9999)
 def test_inactive_configured_bridges_reserved(self):
  with tempfile.TemporaryDirectory() as t:
   t=Path(t);(t/'net'/'vmbr201').mkdir(parents=True);(t/'interfaces.d').mkdir()
   (t/'interfaces').write_text('iface vmbr209 inet manual\n')
   (t/'interfaces.d'/'lab').write_text('auto vmbr210\n')
   used=ranges.bridge_ids(t/'net',t/'interfaces')
   self.assertEqual(used,{201,209,210})
   self.assertEqual(ranges.first_free(used,201,9,9999),211)
 def test_menu_forwards_auto_and_manual_ranges(self):
  for entry,expected in [('1\n0\n',['--base-id','auto','--bridge-base','auto']),('3\n300\n301\n\n\n\n1\n0\n',['--base-id','300','--bridge-base','301'])]:
   with self.subTest(entry=entry), tempfile.TemporaryDirectory() as t:
    t=Path(t);bins=t/'bin';bins.mkdir();sources=t/'source';sources.mkdir()
    for f in ['image-manifest.py','lab-manager.py','detect-uplink.py','select-ranges.py','images.json','module1-images.example.json']:(sources/f).write_text((ROOT/f).read_text())
    (sources/'deploy.sh').write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$MENU_LOG"\n')
    (bins/'qm').write_text('#!/bin/sh\nexit 0\n');(bins/'qm').chmod(0o755)
    (bins/'curl').write_text('#!/usr/bin/env python3\nimport os,sys,shutil\na=sys.argv[1:]\nshutil.copy(os.environ["MENU_SOURCE"]+"/"+a[a.index("-o")-1].split("/")[-1],a[a.index("-o")+1])\n');(bins/'curl').chmod(0o755)
    script=t/'bootstrap.sh';script.write_text((ROOT/'bootstrap.sh').read_text().replace('[[ $EUID == 0 ]]','[[ 1 == 1 ]]').replace('/root/proxmox-demo-lab',str(t/'repo')))
    env=dict(os.environ,PATH=str(bins)+':'+os.environ['PATH'],MENU_LOG=str(t/'args'),MENU_SOURCE=str(sources))
    result=subprocess.run(['bash',str(script),'--menu'],input=entry,text=True,capture_output=True,env=env)
    self.assertEqual(result.returncode,0,result.stderr)
    self.assertEqual((t/'args').read_text().splitlines()[:5],['--plan']+expected)
if __name__=='__main__':unittest.main()
