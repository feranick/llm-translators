# Local (Mac or Linux):
 
To update the app and the pins to the translators, use: this script: `scripts/local-llm/llmt-update.sh`

# Each server (carbonio, mochi, spark1, spark2)
```
cd /opt/llm-translators/src && sudo git pull && sudo git submodule update --init && sudo ./llmt deploy --test
```

This script can be found on `scripts/remote/llmt-update.sh`
Copy this scripts in your remote machines. 
