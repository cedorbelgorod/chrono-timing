#!/bin/bash
# РЎР±РѕСЂРєР° APK Р±РµР· Gradle: aapt2 + javac + d8 + apksigner
set -e
SDK="$HOME/sdk"
BT="$SDK/build-tools/34.0.0"
PLAT="$SDK/platforms/android-34/android.jar"
SRC=/tmp/chrono_app
OUT=/tmp/chrono_build

rm -rf "$OUT"
mkdir -p "$OUT/classes"

cd "$SRC"
echo "== aapt2 compile =="
"$BT/aapt2" compile --dir res -o "$OUT/res.zip"

echo "== aapt2 link =="
"$BT/aapt2" link -o "$OUT/base.apk" -I "$PLAT" \
    --manifest AndroidManifest.xml -R "$OUT/res.zip" --auto-add-overlay

echo "== javac =="
javac -source 8 -target 8 -nowarn -bootclasspath "$PLAT" -d "$OUT/classes" \
    $(find java -name '*.java')

echo "== d8 =="
"$BT/d8" --lib "$PLAT" --min-api 21 --output "$OUT" $(find "$OUT/classes" -name '*.class')

echo "== package =="
cp "$OUT/base.apk" "$OUT/unsigned.apk"
cd "$OUT"
zip -q -j unsigned.apk classes.dex
"$BT/zipalign" -f 4 unsigned.apk aligned.apk

echo "== sign =="
if [ ! -f "$OUT/chrono.keystore" ]; then
    keytool -genkeypair -keystore "$OUT/chrono.keystore" -storepass chrono123 \
        -keypass chrono123 -alias chrono -dname "CN=Chrono, O=Chrono, C=RU" \
        -keyalg RSA -keysize 2048 -validity 10950 > /dev/null 2>&1
fi
"$BT/apksigner" sign --ks "$OUT/chrono.keystore" --ks-pass pass:chrono123 \
    --key-pass pass:chrono123 --out "$OUT/chrono.apk" aligned.apk

echo "== verify =="
"$BT/apksigner" verify "$OUT/chrono.apk" && echo "signature OK"
"$BT/aapt" dump badging "$OUT/chrono.apk" | head -5
ls -la "$OUT/chrono.apk"
