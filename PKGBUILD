pkgname=lunahr
pkgver=2.0.1
pkgrel=1
pkgdesc="Polar H10 heart rate monitor app with OSC and logging"
arch=('x86_64')
url="https://github.com/PetLucy/LunaHR-Linux"
license=('custom')

depends=(
  'python'
  'pyside6'
  'python-pyqtgraph'
  'python-bleak'
  'python-osc'
  'python-colorama'
)

source=("lunahr.py" "lunahr.desktop" "lunahr.png")
sha256sums=('SKIP' 'SKIP' 'SKIP')

package() {
  install -Dm644 lunahr.py "$pkgdir/usr/lib/lunahr/lunahr.py"

  install -Dm755 /dev/stdin "$pkgdir/usr/bin/lunahr" <<'EOF'
#!/usr/bin/env bash
exec /usr/bin/python3 /usr/lib/lunahr/lunahr.py "$@"
EOF

  install -Dm644 lunahr.desktop "$pkgdir/usr/share/applications/lunahr.desktop"
  install -Dm644 lunahr.png "$pkgdir/usr/share/icons/hicolor/128x128/apps/lunahr.png"
}
