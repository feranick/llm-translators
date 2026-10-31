cd ~/Software/llm-translators
git add docs/EXECUTION.md docs/SHARED.md translators.json
git commit -m "Docs: node pinning, stale servers, settings parsing and answering in time implemented by all four" && git push
./llmt pin b3dt feat cfdt omct lmpt --commit --push
