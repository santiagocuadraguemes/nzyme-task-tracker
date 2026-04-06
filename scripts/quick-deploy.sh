#!/usr/bin/env bash
# Quick deploy: update Lambda code without SAM/CloudFormation.
# Use this for code-only changes. For dependency or infra changes, use deploy.sh.
#
# Usage: ./scripts/quick-deploy.sh

set -euo pipefail
cd "$(dirname "$0")/.."

BUILD_DIR=".aws-sam/build/NzymeFunction"

if [ ! -d "$BUILD_DIR" ]; then
    echo "ERROR: No SAM build found. Run 'sam build' first."
    exit 1
fi

echo "=== Copying source files ==="
cp -r src/ "$BUILD_DIR/src/"

echo "=== Zipping ==="
python -c "
import zipfile, os, sys
with zipfile.ZipFile('lambda.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
    root = '$BUILD_DIR'
    for dirpath, dirs, files in os.walk(root):
        for f in files:
            full = os.path.join(dirpath, f)
            arcname = os.path.relpath(full, root)
            zf.write(full, arcname)
print(f'Zipped {os.path.getsize(\"lambda.zip\") / 1024 / 1024:.1f} MB')
"

echo "=== Uploading to Lambda ==="
aws lambda update-function-code \
    --function-name nzyme-task-tracker \
    --zip-file fileb://lambda.zip \
    --no-cli-pager

rm lambda.zip
echo "=== Done ==="
