cask "zcl-teleport" do
  version "1.3.0"
  sha256 "18c5b5d4b306dec733ab794f36cc557d92c7edb019c820638c7228f9c14348a8"

  url "https://github.com/Nha-Cong-Dong/zero-cloud-lan-teleport/releases/download/v#{version}/Teleport-#{version}-macOS.dmg",
      verified: "github.com/Nha-Cong-Dong/zero-cloud-lan-teleport/"
  name "ZCL Teleport"
  desc "Direct LAN file transfer and clipboard sync without cloud servers"
  homepage "https://luffyreals.github.io/zcl-hub/"

  livecheck do
    url :url
    strategy :github_latest
  end

  depends_on macos: ">= :monterey"

  app "Teleport.app"

  zap trash: [
    "~/Library/Application Support/Teleport",
    "~/Library/Caches/Teleport",
    "~/Library/Preferences/io.github.nhacongdong.teleport.plist",
  ]

  caveats <<~EOS
    Bản phát hành macOS hiện chưa qua Apple notarization.
    Nếu macOS chặn khi mở lần đầu, hãy chuột phải vào Teleport.app và chọn Open,
    hoặc chạy:
      xattr -dr com.apple.quarantine /Applications/Teleport.app
  EOS
end
