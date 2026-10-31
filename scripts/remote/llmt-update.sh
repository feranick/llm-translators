#!/bin/bash

cd /opt/llm-translators/src
sudo git pull 
sudo git submodule update --init 
sudo ./llmt deploy $1
