from pattern_engine import PatternEngine

TARGETS=[f"t{i}" for i in range(1,11)]

# Existing v4 semantics: one pass through all targets per WS.
for burst in range(1,11):
    p=PatternEngine(TARGETS, burst=burst, combo='off')
    assert p.jobs_per_socket()==10, (burst,p.jobs_per_socket())

# Existing combo semantics: BRUTE2+BRUTE3 and BRUTE3+BRUTE4 each process
# every target once per component pattern.
assert PatternEngine(TARGETS, combo='combo1').jobs_per_socket()==20
assert PatternEngine(TARGETS, combo='combo2').jobs_per_socket()==20

# Empty target pool must be harmless.
assert PatternEngine([]).jobs_per_socket()==0

print('pattern regression: PASS')
