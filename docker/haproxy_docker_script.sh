cat /etc/haproxy/haproxy.cfg | awk '/^([^ \t].*)$/ { current=$0 };
{ if (match($0, /^[ \t]+[^ \t]/)) { spaces = substr($0, RSTART, RLENGTH-1); } }
{ after_first=is_first; is_first=($0 == current); }
{ if (after_first && current ~ /^frontend +public *$/) {
    # Uncomment this for HTTPS
    #print spaces "bind *:443 ssl crt /etc/haproxy/cert.pem alpn h2,http/1.1"
    #print spaces "http-request redirect scheme https code 301 if !{ hdr(Host) -i 127.0.0.1 } !{ ssl_fc }"
    #print spaces "http-request set-header X-Forwarded-Proto https if { ssl_fc }"
    #print spaces "http-request set-header X-Forwarded-Port %[dst_port]"
  }
  if (after_first && current ~ /^backend +webcam *$/) {
    print spaces "option http-no-delay"
  }
  if (after_first && current ~ /^global *$/) {
      print spaces "tune.rcvbuf.client 4096"
      print spaces "tune.rcvbuf.server 4096"
      print spaces "tune.sndbuf.client 4096"
      print spaces "tune.sndbuf.server 4096"
  }
  print $0
}'
