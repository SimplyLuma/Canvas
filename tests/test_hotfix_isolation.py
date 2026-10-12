"""Keep the manual HOTFIX profile distinct from production Canvas."""
import json
import os
import subprocess
import sys
import tempfile
import unittest


class HotfixIsolation(unittest.TestCase):
    def test_hotfix_uses_a_distinct_identity_and_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ, XDG_DATA_HOME=folder, XDG_STATE_HOME=folder)
            query = ('import json; from luma_canvas.persistence import APP_ID, '
                     'data_home, state_home; print(json.dumps([APP_ID, '
                     'str(data_home()), str(state_home())]))')
            env.pop('LUMA_CANVAS_HOTFIX', None)
            normal = json.loads(subprocess.check_output([sys.executable, '-c', query], env=env))
            env['LUMA_CANVAS_HOTFIX'] = '1'
            hotfix = json.loads(subprocess.check_output([sys.executable, '-c', query], env=env))
            self.assertEqual(normal[0], 'org.projectluma.Canvas')
            self.assertEqual(hotfix[0], 'org.projectluma.Canvas.Hotfix')
            self.assertNotEqual(normal[1:], hotfix[1:])
