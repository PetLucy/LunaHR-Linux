# Maintainer: Your Name <your@email.com>
pkgname=lunahr
pkgver=1.0.0
pkgrel=1
pkgdesc="Heart rate monitor for Polar H10 with VRChat OSC support"
arch=('x86_64')
url="https://github.com/yourusername/lunahr"
license=('custom')
depends=('python' 'pyside6' 'python-pyqtgraph' 'python-bleak' 'python-osc')
makedepends=('pyinstaller')
source=("lunahr.py" "lunahr.png" "lunahr.desktop")
sha256sums=('SKIP' 'SKIP' 'SKIP')

build() {
    cd "$srcdir"
    # Build standalone binary
    pyinstaller --noconsole --onefile --name LunaHR lunahr.py
}

package() {
    cd "$srcdir"

    # Install binary
    install -Dm755 "dist/LunaHR" "$pkgdir/usr/bin/lunahr"

    # Install desktop entry
    install -Dm644 "lunahr.desktop" "$pkgdir/usr/share/applications/lunahr.desktop"

    # Install icon
    install -Dm644 "lunahr.png" "$pkgdir/usr/share/icons/hicolor/128x128/apps/lunahr.png"
}
