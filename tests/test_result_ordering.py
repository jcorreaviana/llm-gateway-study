"""A saída não pode depender da ordem dos arquivos nem das linhas de entrada (o `glob` do Linux não vem ordenado)."""

import json
import random
import shutil

from evalkit import report as rp
from evalkit import score as sc
from tests.test_eval_stats import synthetic_results
from tests.test_report import make_human, make_meta


def mkroot(path):
    path.mkdir(parents=True)
    return path


def shuffled_copy(src_root, dst_root, seed):
    """Copia o run, embaralhando as linhas de cada arquivo e criando os arquivos na ordem inversa."""
    rng = random.Random(seed)
    (dst_root / "r1").mkdir(parents=True)
    for path in sorted((src_root / "r1").glob("*.jsonl"), reverse=True):
        lines = path.read_text(encoding="utf-8").splitlines()
        rng.shuffle(lines)
        (dst_root / "r1" / path.name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_score(root):
    assert sc.main(["--run-id", "r1", "--results-root", str(root), "--no-embedding"]) == 0
    run = root / "r1"
    return (run / "scores.jsonl").read_text(encoding="utf-8"), (run / "summary.json").read_text(encoding="utf-8")


def test_score_output_does_not_depend_on_input_order(tmp_path):
    ordered = synthetic_results(mkroot(tmp_path / "a"))
    shuffled = tmp_path / "b"
    shuffled_copy(ordered, shuffled, seed=7)
    s_a, sum_a = run_score(ordered)
    s_b, sum_b = run_score(shuffled)
    assert sum_a == sum_b  # percentis, bootstrap, McNemar e listas de ids
    assert sorted(s_a.splitlines()) == sorted(s_b.splitlines())  # as mesmas linhas de score


def test_scores_jsonl_has_an_explicit_order(tmp_path):
    root = synthetic_results(mkroot(tmp_path / "a"))
    shuffled = tmp_path / "b"
    shuffled_copy(root, shuffled, seed=11)
    s_a, _ = run_score(root)
    s_b, _ = run_score(shuffled)
    assert s_a == s_b  # nem a ordem das linhas muda


def test_report_does_not_depend_on_input_order(tmp_path):
    ordered = synthetic_results(mkroot(tmp_path / "a"))
    shuffled = tmp_path / "b"
    shuffled_copy(ordered, shuffled, seed=3)
    outputs = []
    for root in (ordered, shuffled):
        run_score(root)
        run = root / "r1"
        meta = make_meta()
        (run / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        human = make_human()
        if root is shuffled:
            random.Random(5).shuffle(human["items"])
        (run / "human_verdicts.json").write_text(json.dumps(human), encoding="utf-8")
        md, cs = rp.execute("r1", results_root=root, out_dir=root / "out", gpu_name="GPU", out=lambda _m: None)
        outputs.append((md.read_text(encoding="utf-8"), cs.read_text(encoding="utf-8")))
    assert outputs[0] == outputs[1]
    shutil.rmtree(tmp_path / "a" / "out")
