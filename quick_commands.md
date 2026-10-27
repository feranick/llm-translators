# Mac: the docs, then make sure all four pins are at the pushed versions (a pin already current is left alone)
cd ~/Software/llm-translators
git add docs/EXECUTION.md docs/SHARED.md translators.json
git commit -m "Docs: node pinning, stale servers, settings parsing and answering in time implemented by all four" && git push
./llmt pin cfdt omct lmpt feat --commit --push

# each server (carbonio, mochi, spark1, spark2)
cd /opt/llm-translators/src && sudo git pull && sudo git submodule update --init && sudo ./llmt deploy --test
