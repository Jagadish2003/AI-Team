"""
Seven detector modules — SF-2.5 implementation.
Each module implements detect(sf_data, sn_data, jira_data) -> List[DetectorResult].
Thresholds are from SF-1.3. All detectors are pure functions — no DB, no network.
"""
