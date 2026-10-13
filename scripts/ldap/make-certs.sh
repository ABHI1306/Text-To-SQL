#!/usr/bin/env sh
# Generates a throwaway CA and a server certificate for the OpenLDAP demo container.
# The image's built-in CA has expired, and clients must verify certificates, so use these.
set -eu
dir="$(dirname "$0")/certs"
mkdir -p "$dir"
cd "$dir"
openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj "/O=Text2SQL demo/CN=Demo LDAP CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout ca.key -out ca.crt
openssl req -newkey rsa:3072 -nodes -subj "/O=Text2SQL demo/CN=ldap.example.org" \
  -keyout ldap.key -out ldap.csr
printf 'subjectAltName=DNS:ldap.example.org,DNS:openldap,DNS:localhost\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' > san.ext
openssl x509 -req -in ldap.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 \
  -extfile san.ext -out ldap.crt
openssl dhparam -out dhparam.pem 2048
rm -f ldap.csr san.ext ca.srl
chmod 644 ca.crt ldap.crt ldap.key dhparam.pem
echo "Certificates written to $dir"
