import boto3
import botocore
# import jsonschema
import json
import traceback
import zipfile
import os

from botocore.exceptions import ClientError

from extutil import remove_none_attributes, account_context, ExtensionHandler, ext, \
    current_epoch_time_usec_num, component_safe_name, lambda_env, random_id, \
    handle_common_errors, create_zip

eh = ExtensionHandler()

codebuild = boto3.client('codebuild')

## REFER TO
## https://docs.aws.amazon.com/codebuild/latest/userguide/available-runtimes.html
## for a list of available runtimes and their matching images

def lambda_handler(event, context):
    try:
        print(f"event = {event}")
        # account_number = account_context(context)['number']
        # region = account_context(context)['region']
        eh.capture_event(event)
        bucket = event.get("bucket")
        object_name = event.get("s3_object_name")

        prev_state = event.get("prev_state") or {}
        project_code = event.get("project_code")
        repo_id = event.get("repo_id")
        cdef = event.get("component_def")
        cname = event.get("component_name")

        project_name = cdef.get("project_name")
        if not project_name:
            eh.add_log("No Project Specified", cdef, is_error=True)
            eh.perm_error("project_name required", 0)
        trust_level = cdef.get("trust_level")

        secondary_artifacts_override = cdef.get("secondary_artifacts_override")
        artifacts_override = cdef.get("artifacts_override") or None
        source_version = cdef.get("source_version") or None
        environment_variables_override = cdef.get("environment_variables_override") or None

        if event.get("pass_back_data"):
            print(f"pass_back_data found")
        elif event.get("op") == "upsert":
            if trust_level == "full":
                eh.add_op("start_build")
            else:
                eh.add_op("get_codebuild_project")

        elif event.get("op") == "delete":
            pass
            
        compare_defs(event)

        build_def = remove_none_attributes({
            "projectName": project_name,
            "artifactsOverride": artifacts_override,
            "secondaryArtifactsOverride": secondary_artifacts_override,
            "sourceVersion": source_version,
            "environmentVariablesOverride": environment_variables_override
        })

        print(f"build_def = {build_def}")

        start_build(build_def)
        check_build_complete()
            
        return eh.finish()

    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()

@ext(handler=eh, op="compare_defs")
def compare_defs(event):
    old_rendef = event.get("prev_state", {}).get("rendef")
    new_rendef = event.get("component_def")

    _ = old_rendef.pop("trust_level", None)
    _ = new_rendef.pop("trust_level", None)

    if old_rendef != new_rendef:
        eh.add_op("start_build")

    else:
        eh.add_links(event.get("prev_state", {}).get('links'))
        eh.add_props(event.get("prev_state", {}).get('props'))
        eh.add_log("Full Trust, No Change: Exiting", {"old": old_rendef, "new": new_rendef})

@ext(handler=eh, op="start_build")
def start_build(build_def):
    codebuild = boto3.client('codebuild')

    try:
        response = codebuild.start_build(**build_def).get("build")
        eh.add_log("Started Build", response)
        eh.add_props({
            "id": response.get("id"),
            "arn": response.get("arn"),
            "number": response.get("buildNumber"),
        })
        eh.add_op("check_build_complete")
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] in ['InvalidInputException', 'ResourceNotFoundException']:
            eh.add_log("Start Build Failed", {"error": str(e)}, True)
            eh.perm_error(str(e), progress=50)
        else:
            eh.add_log("Start Build Error", {"error": str(e)}, True)
            eh.retry_error(str(e), progress=50)

@ext(handler=eh, op="check_build_complete")
def check_build_complete():
    build_id = eh.props.get("id")

    try:
        response = codebuild.batch_get_builds(ids=[build_id]).get("builds")[0]
        if response.get("buildStatus") == "SUCCEEDED":
            eh.add_log("Build Succeeded", response)
        elif response.get("buildStatus") in ["FAILED", "FAULT", "STOPPED", "TIMED_OUT"]:
            eh.add_log("Build Failed", response, is_error=True)
            eh.perm_error("Build Failed", progress=20)
        else:
            eh.add_log("Build In Progress", response)
            eh.retry_error(current_epoch_time_usec_num(), progress=30, callback_sec=8)
    except botocore.exceptions.ClientError as e:
        handle_common_errors(e, eh, "Check Build Error", progress=30)

def format_tags(tags_dict):
    return [{"Key": k, "Value": v} for k,v in tags_dict]

def unformat_tags(tags_list):
    return {t["Key"]: t["Value"] for t in tags_list}


"""
aws/codebuild/amazonlinux2-x86_64-standard:3.0	
AMAZON LINUX 2 AVAILABILITY:
version: 0.1

runtimes:
  android:
    versions:
      28:
        requires:
          java: ["corretto8"]
        commands:
          - echo "Installing Android version 28 ..."
      29:
        requires:
          java: ["corretto8"]
        commands:
          - echo "Installing Android version 29 ..."
  java:
    versions:
      corretto11:
        commands:
          - echo "Installing corretto(OpenJDK) version 11 ..."

          - export JAVA_HOME="$JAVA_11_HOME"

          - export JRE_HOME="$JRE_11_HOME"

          - export JDK_HOME="$JDK_11_HOME"

          - |-
            for tool_path in "$JAVA_HOME"/bin/*;
             do tool=`basename "$tool_path"`;
              if [ $tool != 'java-rmi.cgi' ];
              then
               rm -f /usr/bin/$tool /var/lib/alternatives/$tool \
                && update-alternatives --install /usr/bin/$tool $tool $tool_path 20000;
              fi;
            done
      corretto8:
        commands:
          - echo "Installing corretto(OpenJDK) version 8 ..."

          - export JAVA_HOME="$JAVA_8_HOME"

          - export JRE_HOME="$JRE_8_HOME"

          - export JDK_HOME="$JDK_8_HOME"

          - |-
            for tool_path in "$JAVA_8_HOME"/bin/* "$JRE_8_HOME"/bin/*;
             do tool=`basename "$tool_path"`;
              if [ $tool != 'java-rmi.cgi' ];
              then
               rm -f /usr/bin/$tool /var/lib/alternatives/$tool \
                && update-alternatives --install /usr/bin/$tool $tool $tool_path 20000;
              fi;
            done
  golang:
    versions:
      1.12:
        commands:
          - echo "Installing Go version 1.12 ..."
          - goenv global  $GOLANG_12_VERSION
      1.13:
        commands:
          - echo "Installing Go version 1.13 ..."
          - goenv global  $GOLANG_13_VERSION
      1.14:
        commands:
          - echo "Installing Go version 1.14 ..."
          - goenv global  $GOLANG_14_VERSION
  python:
    versions:
      3.9:
        commands:
          - echo "Installing Python version 3.9 ..."
          - pyenv global  $PYTHON_39_VERSION
      3.8:
        commands:
          - echo "Installing Python version 3.8 ..."
          - pyenv global  $PYTHON_38_VERSION
      3.7:
        commands:
          - echo "Installing Python version 3.7 ..."
          - pyenv global  $PYTHON_37_VERSION
  php:
    versions:
      7.4:
        commands:
          - echo "Installing PHP version 7.4 ..."
          - phpenv global $PHP_74_VERSION
      7.3:
        commands:
          - echo "Installing PHP version 7.3 ..."
          - phpenv global $PHP_73_VERSION
  ruby:
    versions:
      2.6:
        commands:
          - echo "Installing Ruby version 2.6 ..."
          - rbenv global $RUBY_26_VERSION
      2.7:
        commands:
          - echo "Installing Ruby version 2.7 ..."
          - rbenv global $RUBY_27_VERSION
  nodejs:
    versions:
      10:
        commands:
          - echo "Installing Node.js version 10 ..."
          - n $NODE_10_VERSION
      12:
        commands:
          - echo "Installing Node.js version 12 ..."
          - n $NODE_12_VERSION
  docker:
    versions:
      18:
        commands:
          - echo "Using Docker 19"
      19:
        commands:
          - echo "Using Docker 19"
  dotnet:
    versions:
      3.1:
        commands:
          - echo "Installing .NET version 3.1 ..."
"""

    

