-- Buck/doe score per deer crop, written by stages/sex.py from the monthly model
-- (training/sex/, `current` run).
--
-- buck_prob is P(buck) for this crop: mean of original and mirror. A capture's score is the
-- max over its blacktail crops ("at least one buck present", how the M tag is defined), the
-- same rule train_eval.py evaluates with. NULL = not scored (not blacktail, or no model yet).
-- sex_model is the run directory name (runs/<date>) that produced the score (invariant 2:
-- every derived record carries the model identity that produced it).

ALTER TABLE detections ADD COLUMN buck_prob REAL;
ALTER TABLE detections ADD COLUMN sex_model TEXT;
ALTER TABLE detections ADD COLUMN sex_scored_at TEXT;
