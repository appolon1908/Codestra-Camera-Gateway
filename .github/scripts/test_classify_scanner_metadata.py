import copy, hashlib, importlib.util, json, os, pathlib, subprocess, tempfile, unittest
from unittest.mock import patch
import classify_scanner_metadata as m

class Classification(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.old=os.getcwd();os.chdir(self.tmp.name);self.addCleanup(os.chdir,self.old)
  self.g('init','-q');self.g('config','user.email','test@example.invalid');self.g('config','user.name','Test')
  pathlib.Path('known.txt').write_text('nonsecret deterministic source\n');digest=hashlib.sha256(pathlib.Path('known.txt').read_bytes()).hexdigest()
  self.g('add','.');self.g('commit','-qm','base');parent=self.g('rev-parse','HEAD').strip()
  raw=('"metadata"\nimport hashlib\n\nREPOSITORY = "owner/test"\nBASE = '+repr(parent)+'\nEXPECTED = '+repr({'known.txt':digest})+'\n').encode()
  pathlib.Path(m.PATH).parent.mkdir(parents=True);pathlib.Path(m.PATH).write_bytes(raw);self.g('add','.');self.g('commit','-qm','metadata');commit=self.g('rev-parse','HEAD').strip()
  self.receipt=dict(repository='owner/test',commit=commit,parent=parent,blob=self.g('rev-parse','HEAD:'+m.PATH).strip(),sha256=hashlib.sha256(raw).hexdigest(),line_sha256=hashlib.sha256(raw.splitlines()[5]).hexdigest(),expected={'known.txt':digest})
  self.item=dict(File=m.PATH,RuleID='generic-api-key',StartLine=6,EndLine=6,Commit=commit,Fingerprint=commit+':'+m.PATH+':generic-api-key:6')
 def g(self,*args):return subprocess.check_output(['git',*args]).decode()
 def run_case(self,items=None,status=1,kind='history',receipt=None):return m.classify('owner/test',kind,status,[self.item] if items is None else items,[receipt or self.receipt])
 def test_exact_actual_git(self):self.assertEqual(self.run_case()[2],0)
 def test_tree_exact(self):
  self.item['Commit']='';self.item['Fingerprint']=m.PATH+':generic-api-key:6';self.assertEqual(self.run_case(kind='tree')[2],0)
 def test_adjacent_secret_retained(self):
  other=dict(self.item,StartLine=7,Fingerprint=self.item['Fingerprint']+'other');out=self.run_case([self.item,other]);self.assertEqual(out[0],[other]);self.assertEqual(out[2],1)
 def test_duplicate_rejected(self):
  with self.assertRaises(ValueError):self.run_case([self.item,self.item])
 def test_error_status(self):
  for status in (2,-1,True):
   with self.assertRaises(ValueError):self.run_case(status=status)
 def test_registered_no_legacy_permission(self):
  r={'repository':'owner/test','parent':self.receipt['parent'],'legacy_metadata_authorized':False}
  self.assertEqual(self.run_case(receipt=r)[0],[self.item])
  self.assertEqual(self.run_case([],0,receipt=r)[2],0)
 def test_missing_endline_retained(self):
  item=dict(self.item);del item['EndLine'];self.assertEqual(self.run_case([item])[0],[item])
 def test_empty_success(self):self.assertEqual(self.run_case([],0)[2],0)
 def test_status_mismatch(self):
  with self.assertRaises(ValueError):self.run_case(status=0)
 def test_empty_failure(self):
  with self.assertRaises(ValueError):self.run_case([],1)
 def test_unknown_repo(self):
  with self.assertRaises(ValueError):m.classify('unknown','history',1,[self.item],[self.receipt])
 def test_tampered_provenance(self):
  for key in ('blob','sha256','line_sha256','parent','expected'):
   r=copy.deepcopy(self.receipt);r[key]={} if key=='expected' else '0'*40
   with self.assertRaises(ValueError):self.run_case(receipt=r)
 def test_identity_variations_retained(self):
  for key,value in [('File','other.py'),('RuleID','private-key'),('StartLine',7),('StartLine',True),('EndLine',7),('EndLine',True),('EndLine',None),('Commit','0'*40),('Fingerprint','unknown')]:
   item=dict(self.item);item[key]=value;self.assertEqual(self.run_case([item])[0],[item])
 def test_malformed(self):
  with self.assertRaises(ValueError):self.run_case(['bad'])
 def test_duplicate_registry(self):
  with self.assertRaises(ValueError):m.classify('owner/test','history',1,[self.item],[self.receipt,self.receipt])
 def test_effective_tree_changed(self):
  self.item['Commit']='';self.item['Fingerprint']=m.PATH+':generic-api-key:6';pathlib.Path(m.PATH).write_text('changed')
  with self.assertRaises(ValueError):self.run_case(kind='tree')
if __name__=='__main__':unittest.main()
