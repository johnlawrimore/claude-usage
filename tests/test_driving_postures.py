"""Policy contracts for consuming available quota without overspending it."""
import json
from test_check_usage import CheckUsageTestCase

class TestDrivingPostures(CheckUsageTestCase):
    def limit(self, percent=50, remaining=9000, ratio=None, kind='session'):
        out = dict(kind=kind, scope_model=None, percent=percent, resets_in_seconds=remaining)
        if ratio is not None:
            out['burn'] = dict(delta_percent=ratio * (100-percent) / remaining * 2000,
                               over_seconds=2000)
        return out

    def test_five_colors_and_meanings(self):
        self.assertEqual(self.mod.LEVELS, ['accelerate','cruise','coast','brake','park'])
        self.assertEqual([self.mod.POSTURE_COLORS[p] for p in self.mod.LEVELS],
                         ['blue','green','yellow','orange','red'])
        for posture in self.mod.LEVELS:
            self.assertTrue(self.mod.POSTURE_INSTRUCTIONS[posture])

    def test_recent_boundaries(self):
        for ratio, expected in [(0.5,'accelerate'),(.899,'accelerate'),(.9,'cruise'),
                                (1,'cruise'),(1.001,'coast'),(1.25,'coast'),(1.251,'brake')]:
            with self.subTest(ratio=ratio):
                self.assertEqual(self.mod.limit_posture(self.limit(remaining=15000, ratio=ratio))[0], expected)

    def test_park_is_checkpoint_reserve_not_high_multiplier(self):
        self.assertNotEqual(self.mod.limit_posture(self.limit(ratio=10))[0], 'park')
        self.assertEqual(self.mod.limit_posture(self.limit(percent=90,ratio=40))[0], 'park')
        self.assertEqual(self.mod.limit_posture(self.limit(percent=100))[0], 'park')

    def test_recent_recovery_can_override_old_sprint(self):
        recovered = self.limit(percent=80, remaining=9000, ratio=.5)
        self.assertEqual(self.mod.limit_posture(recovered)[0], 'accelerate')
        recovered.pop('burn')
        self.assertEqual(self.mod.limit_posture(recovered)[0], 'brake')

    def test_expansion_needs_evidence_and_every_scope(self):
        fresh = self.limit(percent=0, remaining=17999)
        spare = self.limit(percent=10)
        self.assertEqual(self.mod.limit_posture(fresh)[0], 'cruise')
        self.assertEqual(self.mod.limit_posture(spare)[0], 'accelerate')
        self.assertEqual(self.mod.overall_posture([spare, fresh])[0], 'cruise')
        self.assertEqual(self.mod.overall_posture([spare, self.limit(kind='unknown')])[0], 'cruise')
        scoped = self.limit(percent=100, kind='weekly_scoped')
        self.assertEqual(self.mod.overall_posture([spare, scoped])[0], 'park')

    def test_short_reset_cannot_turn_overspend_into_accelerate(self):
        limit = self.limit(percent=20, remaining=15000, ratio=1.5)
        self.assertEqual(self.mod.limit_posture(limit)[0], 'coast')

    def test_legacy_hysteresis_state_cannot_survive_policy_migration(self):
        self.mod.save_cache({'fetched_at': self.mod.NOW, 'data': {},
                            'posture_state':{'posture':'wind_down','at':self.mod.NOW}})
        self.assertEqual(self.mod.hysteresis_posture('cruise', None)[0], 'cruise')

    def test_render_instructs_existing_agents(self):
        _, out = self.run_with_exit(self.mod.render, [self.limit(percent=10)], 0, False, 'cache')
        data = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(data['posture'], 'accelerate')
        self.assertEqual(data['posture_policy'], 'ride_limit_v1')
        self.assertIn('Never invent work', data['posture_instruction'])
