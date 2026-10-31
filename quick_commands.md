# Local (Mac or Linux):
 
```
cd ~/Software/llm-translators
git add docs/EXECUTION.md docs/SHARED.md translators.json
git commit -m "Docs: node pinning, stale servers, settings parsing and answering in time implemented by all four" && git push
./llmt pin b3dt feat cfdt omct lmpt --commit --push
```

This script can be found on `scripts/local-llm/llmt-update.sh`
Copy this scripts in your local machine where you run your llm. 
# Each server (carbonio, mochi, spark1, spark2)
```
cd /opt/llm-translators/src && sudo git pull && sudo git submodule update --init && sudo ./llmt deploy --test
```

This script can be found on `scripts/remote/llmt-update.sh`
Copy this scripts in your remote machines. 
