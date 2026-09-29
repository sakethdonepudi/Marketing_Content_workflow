import os
import tempfile
import unittest
from pathlib import Path
import app
class WorkflowTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); app.DB=Path(self.temp.name)/'test.sqlite3'; app.init()
 def tearDown(self): self.temp.cleanup()
 def test_state_and_audit(self):
  eid=app.create_event('Event','Source'); app.transition(eid,'VERIFYING'); app.transition(eid,'VERIFIED')
  with app.connect() as c:
   self.assertEqual(c.execute('SELECT status FROM events WHERE id=?',(eid,)).fetchone()['status'],'VERIFIED')
   self.assertEqual(c.execute('SELECT COUNT(*) FROM transitions WHERE event_id=?',(eid,)).fetchone()[0],3)
 def test_reject_invalid_jump(self):
  eid=app.create_event('Event','Source')
  with self.assertRaises(ValueError): app.transition(eid,'PUBLISHED')
if __name__=='__main__': unittest.main()
