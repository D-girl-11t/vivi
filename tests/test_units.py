"""Unit tests that need no kernel tree:  python3 -m unittest tests.test_units -v"""
import os
import tempfile
import unittest

from server.board import parse_scan
from server.dts import DtsIndex, parse_file, strip_comments

SOC_DTSI = '''
/ {
	soc {
		i2c1: i2c@1000 { compatible = "acme,i2c"; status = "disabled"; };
		i2c2: i2c@2000 { compatible = "acme,i2c"; status = "disabled"; };
	};
};
'''
BOARD = '''/dts-v1/;
#include "soc.dtsi"
/* &i2c2 { status = "okay"; };  commented out, must be ignored */
&i2c1 {
	status = "okay";   // enabled by the board
	sensor: temp@48 { compatible = "acme,temp"; reg = <0x48>; };
};
'''


class DtsTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, 'dts'))
        open(os.path.join(self.d, 'dts/soc.dtsi'), 'w').write(SOC_DTSI)
        open(os.path.join(self.d, 'dts/board.dts'), 'w').write(BOARD)
        self.ix = DtsIndex(self.d, 'dts/board.dts', 'dts')

    def test_include_order(self):
        self.assertEqual([os.path.basename(f.path) for f in self.ix.files], ['soc.dtsi', 'board.dts'])

    def test_effective_status_and_comments(self):
        self.assertEqual(self.ix.label_info('i2c1')['status'], 'okay')
        self.assertEqual(self.ix.label_info('i2c2')['status'], 'disabled')   # the commented-out override is ignored
        self.assertFalse(self.ix.label_info('nope')['exists'])

    def test_compatible_and_board_block(self):
        hit = self.ix.find_compatible('acme,temp')
        self.assertEqual(hit[0]['node'], 'sensor: temp@48')
        self.assertEqual(self.ix.board_block('&i2c1')['line'], 4)

    def test_strip_keeps_offsets(self):
        s = 'a /* x\ny */ b // z\nc "/* not a comment */"'
        out = strip_comments(s)
        self.assertEqual(len(out), len(s)); self.assertEqual(out.count('\n'), 2)
        self.assertIn('"/* not a comment */"', out)

    def test_paths(self):
        pf = parse_file(os.path.join(self.d, 'dts/soc.dtsi'))
        self.assertEqual(pf.path_of(pf.nodes[-1]), '/soc/i2c@2000')


class ScanTests(unittest.TestCase):
    def test_unbound_detection(self):
        raw = '\n'.join([
            '===S uname===', 'Linux test', '===S dt===',
            '/soc/i2c@1000\tacme,i2c|\tokay', '/soc/i2c@2000\tacme,i2c|\tokay', '/soc/spi@3000\tacme,spi|\tdisabled',
            '===S devices===',
            'platform\t1000.i2c\ti2c-acme\t/soc/i2c@1000', 'platform\t2000.i2c\t\t/soc/i2c@2000',
            '===S kconfig===', 'CONFIG_FOO=y', '===S end===', ''])
        s = parse_scan(raw)
        self.assertEqual(s['findings']['bound'], 1)
        self.assertEqual([u['path'] for u in s['findings']['unbound']], ['/soc/i2c@2000'])
        self.assertEqual(s['kconfig'], {'CONFIG_FOO': 'y'})


if __name__ == '__main__':
    unittest.main()
