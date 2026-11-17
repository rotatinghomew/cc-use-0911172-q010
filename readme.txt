产业岗位与青年人才供需流动观测仓
测试：python3 -m unittest discover -s tests -v
构建：python3 -m compileall -q talent_observatory tests
验收：python3 -m talent_observatory --database /tmp/talent_observatory.sqlite3 run --fixture fixtures/acceptance.json --output /tmp/talent_observatory-output.json
单测：python3 -m unittest tests.test_domain -v
技能供需兑现分析：talent_observatory/metrics/realization.py（Reporter.skill_realization，按结业期到后续岗位窗口；单测 tests.test_skill_realization）
