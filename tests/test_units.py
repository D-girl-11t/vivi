"""Unit tests that need no kernel tree:  python3 -m unittest tests.test_units -v"""
import os
import tempfile
import unittest

from server.board import parse_scan
from server.dts import DtsIndex, parse_file, strip_comments
from server.kernel import driver_requirements

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


CAM_BOARD = BOARD + '''
&i2c1 {
	old_cam: cam@3c {
		compatible = "acme,oldcam";
		port { old_ep: endpoint { remote-endpoint = <&csi_in>; }; };
	};
};
'''

DRIVER_C = r'''
#define FOO_XCLK_FREQ		24000000
#define FOO_LINK_FREQ		408000000
static const s64 foo_link_freq_menu[] = { FOO_LINK_FREQ, };
static const char * const foo_supply_name[] = { "VANA", "VDIG" };
static int foo_probe(struct i2c_client *client)
{
	foo->xclk = devm_clk_get(dev, NULL);
	if (clk_get_rate(foo->xclk) != FOO_XCLK_FREQ) return -EINVAL;
	foo->reset = devm_gpiod_get_optional(dev, "reset", GPIOD_OUT_HIGH);
	foo->pwr = devm_gpiod_get(dev, "powerdown", GPIOD_OUT_HIGH);
	device_property_read_u32(dev, "acme,mode", &mode);
	if (v4l2_fwnode_endpoint_alloc_parse(ep, &cfg)) return -EINVAL;
	if (cfg.bus.mipi_csi2.num_data_lanes != 2) return -EINVAL;
}
static const struct of_device_id foo_dt_ids[] = { { .compatible = "acme,foo" }, { } };
static struct i2c_driver foo_driver = { .probe_new = foo_probe };
'''


class DriverTests(unittest.TestCase):
    def test_requirements_from_source(self):
        r = driver_requirements(DRIVER_C)
        self.assertEqual(r['compatibles'], ['acme,foo'])
        self.assertEqual(r['supply'], ['VANA', 'VDIG'])
        self.assertEqual(r['gpio'], ['reset', 'powerdown'])
        self.assertIn('reset', r['optional']); self.assertNotIn('powerdown', r['optional'])
        self.assertEqual(r['property'], ['acme,mode'])
        self.assertEqual((r['clock_hz'], r['link_freq_values'], r['lanes'], r['bus']), (24000000, [408000000], ['2'], 'i2c'))
        self.assertTrue(r['endpoint'])
        self.assertTrue(any('probe_new' in h for h in r['port_hints']))

    def test_owners_finds_device_behind_endpoint(self):
        d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, 'dts'))
        open(os.path.join(d, 'dts/soc.dtsi'), 'w').write(SOC_DTSI)
        open(os.path.join(d, 'dts/board.dts'), 'w').write(CAM_BOARD)
        ix = DtsIndex(d, 'dts/board.dts', 'dts')
        own = ix.owners(r'remote-endpoint\s*=\s*<&csi_in>')
        self.assertEqual([(o['label'], o['match_label']) for o in own], [('old_cam', 'old_ep')])
        self.assertEqual(ix.owners(r'remote-endpoint\s*=\s*<&csi_in>', exclude_label='old_cam'), [])
        self.assertIn('acme,oldcam', ix.node_body_for_compatible('acme,oldcam')['text'])


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
