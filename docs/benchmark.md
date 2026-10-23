# Benchmark

> **Vai trò:** số đo thực tế của các yêu cầu phi chức năng.
> **Nguồn duy nhất cho:** định nghĩa từng phép đo, môi trường, kết quả từng lần đo và kết luận so với ngưỡng.
> **Không chứa:** ngưỡng yêu cầu → [mvp.md](mvp.md) mục 9 (tham chiếu theo mã `mvp N1`…, không chép số). Thiết kế khám phá, TTL, chu kỳ kiểm còn sống và cận trên đã tính → [protocol.md](protocol.md) mục 8. Trạng thái giai đoạn → [ke_hoach.md](ke_hoach.md) mục 5.1.
> **Cập nhật:** 2026-10-23

## 1. Quy ước

- Mỗi lần đo là một mục con riêng, ghi ngày, commit, môi trường hai phía và script đã dùng. Lần đo cũ giữ nguyên, không sửa số.
- Thống kê: số lần hợp lệ, trung vị, p95, nhỏ nhất, lớn nhất; p95 theo hạng gần nhất (với 10 lần thì bằng lớn nhất). Lần hỏng ghi riêng kèm lý do: **quá hạn** (không thấy sự kiện trong thời gian chờ, tính là không đạt) hoặc **lỗi đo** (môi trường hỏng, không tính vào kết quả).
- Mỗi giá trị là **cận trên** (bảo thủ); khi có, ghi cả cận dưới. Kết luận dùng cận trên: **ĐẠT** khi đủ số lần hợp lệ tối thiểu, không lần nào quá hạn và mọi cận trên dưới ngưỡng; **KHÔNG ĐẠT** khi có lần quá hạn hoặc cận dưới chạm ngưỡng; còn lại **CHƯA ĐỦ DỮ LIỆU**.
- Không có số thật thì ghi **chưa đo**, không bao giờ điền số ước lượng.
- Đo trên máy ảo hoặc môi trường khác máy thật thì ghi rõ giới hạn ngay trong lần đo đó.
- Số thập phân viết bằng dấu phẩy; thời gian tính bằng giây (s) trừ khi ghi ms.

## 2. Tổng hợp

| Mã | Lần đo gần nhất | Kết luận |
|---|---|---|
| mvp N1 | 2026-10-23: host Windows 11 + máy ảo Ubuntu bridged trên cùng máy (mục 3.3) | **ĐẠT trên máy ảo bridged**; chưa đo trên hai máy thật (mục 3.4) |
| mvp N2 | — | chưa đo |
| mvp N3 | — | chưa đo |
| mvp N4 | — | chưa đo |
| mvp N5 | — | chưa đo |
| mvp N6 | — | chưa đo |
| mvp N7 | — | chưa đo |
| mvp N8 | — | chưa đo |
| mvp N9 | — | chưa đo |
| mvp N10 | — | chưa đo |
| mvp N11 | — | chưa đo |
| mvp N12 | — | chưa đo |

## 3. mvp N1: Khám phá

### 3.1. Định nghĩa phép đo

Mọi khoảng thời gian tính trên **một đồng hồ**: `time.perf_counter()` của host. Máy ảo chỉ gửi về *khoảng* thời gian trên đồng hồ của nó (từ lúc nhận lệnh tới lúc bắt đầu quảng bá, bắt đầu `Node.close()`, giết tiến trình), không bao giờ so mốc của hai máy với nhau. Mốc “thấy” / “mất” là lúc `Node` phát `DeviceOnline` / `DeviceOffline` (`teleport.app.node`, gọi đồng bộ trên event loop).

| Ca | Đo từ | Đo tới | Cận dưới | So với phần nào của mvp N1 |
|---|---|---|---|---|
| `appear_vm` | node mới (danh tính mới) trên VM bắt đầu quảng bá (vào `MdnsDiscovery.advertise`) | node trên host phát `DeviceOnline` | trừ RTT kênh điều khiển | xuất hiện |
| `appear_host` | node mới trên host bắt đầu quảng bá | node trên VM phát `DeviceOnline`, mốc lúc host nhận báo | trừ RTT | xuất hiện |
| `open_host` *(bổ sung)* | gọi `Node.start()` trên host khi VM đang quảng bá | host phát `DeviceOnline` cho VM | (một đồng hồ, không qua kênh) | xuất hiện |
| `exit_vm` | gọi `Node.close()` trên VM (gửi goodbye) | host phát `DeviceOffline` | trừ RTT | thoát app |
| `exit_host` | gọi `Node.close()` trên host | VM phát `DeviceOffline`, mốc lúc host nhận báo | trừ RTT | thoát app |
| `link_down` | ngay trước lệnh hypervisor cắt liên kết card mạng VM | host phát `DeviceOffline` | tính từ lúc lệnh hypervisor trả về | mất mạng đột ngột hoặc ngủ |
| `pause_vm` | ngay trước lệnh hypervisor tạm dừng VM (giả lập ngủ / treo) | host phát `DeviceOffline` | tính từ lúc lệnh trả về | mất mạng đột ngột hoặc ngủ |
| `kill_vm` *(bổ sung)* | giết cây tiến trình `teleport listen` trên VM (không goodbye) | host phát `DeviceOffline` | trừ RTT | mất mạng đột ngột hoặc ngủ |
| `kill_host` *(bổ sung)* | giết cây tiến trình `teleport listen` trên host | VM phát `DeviceOffline`, mốc lúc host nhận báo | trừ RTT | mất mạng đột ngột hoặc ngủ |
| `link_down_reappear`, `pause_vm_reappear` | ngay trước lệnh cắm lại mạng / cho VM chạy tiếp | host phát `DeviceOnline` lại | tính từ lúc lệnh trả về | chỉ ghi nhận (mvp N1 không có ngưỡng cho việc này) |

Ca *bổ sung* là cách hiểu thêm của người đo, không thay ca chính: `open_host` là “mở app thấy máy đã có”; `kill_*` là app bị giết, với máy bên kia giống hệt mất mạng đột ngột.

### 3.2. Phương pháp

- Hai node đo đạc dùng API có sẵn (`Node`, `MdnsOptions`; `teleport listen --json` cho ca `kill_*`), thư mục cấu hình tạm riêng, lắng nghe 127.0.0.1 cổng ngẫu nhiên, không ghép đôi, không tự kết nối (`accept_pairing=False`, `auto_connect=False`); mDNS quảng bá và duyệt như `teleport listen`. Ca xuất hiện dùng danh tính mới mỗi lần (thiết bị máy kia chưa từng thấy).
- Host chạy `n1_host.py` (điều phối, giữ mọi mốc); VM chạy `n1_vm_agent.py` (bật/tắt node theo lệnh qua TCP, đẩy sự kiện về ngay). Trước mỗi lần đo, host ping agent 3 lần để có RTT; độ trễ một chiều của kênh nằm trong khoảng 0…RTT.
- Mốc bắt đầu quảng bá / bắt đầu đóng mDNS lấy bằng một lớp con của `MdnsDiscovery` gắn vào `teleport.app.node.MdnsDiscovery` trong tiến trình đo; `src/` không sửa.
- Ca mất mạng / ngủ: hypervisor cắt liên kết (VirtualBox `controlvm … setlinkstateN off`; script cũng hỗ trợ VMware `vmrun disconnectNamedDevice` và Hyper-V `Disconnect-VMNetworkAdapter`) hoặc tạm dừng VM (`pause`; Hyper-V `Suspend-VM`). Sau khi cắt, host ping agent: còn trả lời thì lần đó là lỗi đo (lệnh không có hiệu lực). Lệnh hypervisor chạy trong thread để không làm trễ mốc sự kiện.
- Trước mỗi lần cắt / giết, chờ ngẫu nhiên (phân bố đều, hạt giống ghi trong kết quả) trong một khoảng rộng bằng TTL bản ghi (protocol mục 8), để mẫu phủ mọi pha của TTL và của nhịp kiểm còn sống. Giữa lúc xuất hiện và lúc thoát cũng chờ ngẫu nhiên vài giây (ghi trong `settle_s`).
- Mỗi ca lặp 10 lần; mọi lần chờ có giới hạn; cả lượt đo có giới hạn thời gian; kết thúc (kể cả Ctrl+C) thì cắm lại mạng, cho VM chạy tiếp, tắt mọi node và xóa thư mục tạm.
- Dữ liệu thô: JSON Lines (`results.jsonl` trên host, `agent.jsonl` trên VM), gồm môi trường hai phía, cấu hình, ngưỡng đọc từ `mvp.md` lúc chạy, mọi sự kiện và mọi lần đo.

**Nguồn sai số**

| Nguồn | Xử lý |
|---|---|
| Độ trễ một chiều của kênh điều khiển (ca có VM nhận lệnh hoặc VM báo về) | Đo RTT trước mỗi lần; ghi cận trên (gồm trễ) và cận dưới (trừ RTT lớn nhất) |
| Thời gian chạy lệnh hypervisor (ca `link_down`, `pause_vm`) | Kẹp giữa mốc trước và sau lệnh; ghi thời gian lệnh (`hv_call_s`) |
| Pha của TTL bản ghi và nhịp kiểm còn sống lúc cắt | Chờ ngẫu nhiên trước khi cắt, hạt giống ghi lại |
| Trễ event loop của node quan sát | Là một phần của điều cần đo (mốc lấy ngay khi `Node` phát sự kiện) |
| Đồng hồ máy ảo khi đo khoảng ngắn trên VM | Chỉ dùng cho khoảng vài ms–vài giây ngay sau khi nhận lệnh; không dùng qua lúc VM bị tạm dừng |
| Host và VM dùng chung CPU | Ghi số CPU/RAM của VM; xem giới hạn bên dưới |

### 3.3. Lần đo 2026-10-23: host Windows 11 + máy ảo bridged

Người dùng đã chấp nhận máy ảo bridged thay máy thật thứ hai cho lần đo này. Mã đo: `main` tại commit `efa9d5b`, không sửa (`src_dirty: false` ở cả hai phía). Script: `n1_host.py`, `n1_vm_agent.py`, `n1_common.py` (mục 3.5).

| Lượt | Ca | Thời lượng | Hạt giống |
|---|---|---|---|
| preflight (03:47 UTC) | kiểm kênh agent, hypervisor, mDNS hai chiều | — | — |
| `run-20261023T034748255Z` | `appear_vm`, `exit_vm`, `appear_host`, `exit_host`, `open_host` | 322,6 s | 992322527 |
| `run-20261023T035326280Z` | `kill_vm`, `kill_host` | 1860,2 s | 4130361009 |
| `run-20261023T042452185Z` | `link_down`, `link_down_reappear`, `pause_vm`, `pause_vm_reappear` | 2098,2 s | 780602171 |

**Môi trường** (ghi tự động trong `results.jsonl`, giống nhau ở cả ba lượt trừ tốc độ liên kết Wi-Fi)

| | Host | Máy ảo |
|---|---|---|
| Hệ điều hành | Windows 11, bản 10.0.26300 | Ubuntu 26.04.1 LTS (theo người dùng); `results.jsonl` ghi kernel Linux 7.0.0-38-generic, glibc 2.43. Trường `ostype` của VirtualBox ghi “Ubuntu 25.04 (64-bit)”, là kiểu khai báo lúc tạo VM |
| Python | 3.14.5 | 3.14.4 |
| Teleport | `__version__` 0.0.1, commit `efa9d5bb35a60bb0c7855703c6e62fc464f45b8c`, digest mã nguồn `30a05fd463396837fcaa037fc5c7b777fdbd5b4b94f31ef55bdf62ab75257e22` | giống host (cùng commit, cùng digest) |
| zeroconf / ifaddr / cryptography | 0.151.5 / 0.2.0 / 50.0.2 | 0.151.5 / 0.2.0 / 50.0.2 |
| CPU | Intel Core i7-12800H, 20 luồng | cùng CPU, 2 vCPU |
| RAM | 15,7 GiB | cấp 4096 MB, hệ điều hành khách thấy 3,3 GiB |
| Card mạng | Wi-Fi: Intel Wi-Fi 6 AX201 160MHz, liên kết 103–143 Mbps tùy lượt; hồ sơ mạng Public; card dây Intel I219-LM không cắm | `enp0s3`, driver `e1000` (VirtualBox 82540EM), bridged lên card Wi-Fi AX201 của host |
| Địa chỉ quảng bá | `172.18.96.1`, `192.168.1.7` (xem phần Ghi nhận bên dưới) | `192.168.1.3` |
| Hypervisor | VirtualBox 7.2.16 r174877, VM 2 vCPU, 4096 MB, mạng bridged; Guest Additions trong VM báo 6.0.0 r127566 | — |
| Đồng hồ | `QueryPerformanceCounter()`, độ phân giải 100 ns | `CLOCK_MONOTONIC`, độ phân giải 1 ns |

**RTT kênh điều khiển** (host ↔ agent trong VM, TCP): preflight 10 lần, trung vị 1,79 ms, lớn nhất 5,07 ms. Trong các lượt đo, lấy RTT lớn nhất của 3 ping trước mỗi lần đo có dùng kênh (60 lần): trung vị 4,13 ms, nhỏ nhất 1,34 ms, lớn nhất 64,87 ms.

**Thời gian lệnh hypervisor** (`hv_call_s`): cắt liên kết 0,036–0,116 s; cắm lại 0,037–0,190 s; tạm dừng 0,039–0,081 s; chạy tiếp 0,029–0,077 s. Thời gian chờ ngẫu nhiên trước khi cắt / giết thực tế nằm trong 11,3–68,8 s.

**Kết quả** (giây, cận trên, trên đồng hồ của host; “cận dưới lớn nhất” là giá trị lớn nhất trong các cận dưới)

| Ca | Hợp lệ / hỏng | Trung vị | p95 | Min | Max | Cận dưới lớn nhất | Kết luận |
|---|---|---|---|---|---|---|---|
| `appear_vm` | 10 / 0 | 1,101 | 1,161 | 1,048 | 1,161 | 1,157 | ĐẠT |
| `appear_host` | 10 / 0 | 1,075 | 1,140 | 1,036 | 1,140 | 1,137 | ĐẠT |
| `open_host` *(bổ sung)* | 10 / 0 | 0,107 | 0,144 | 0,068 | 0,144 | — | ĐẠT |
| `exit_vm` | 10 / 0 | 0,003 | 0,006 | 0,002 | 0,006 | 0,001 | ĐẠT |
| `exit_host` | 10 / 0 | 0,005 | 0,008 | 0,004 | 0,008 | 0,004 | ĐẠT |
| `link_down` | 10 / 0 | 61,847 | 82,518 | 23,355 | 82,518 | 82,451 | ĐẠT |
| `pause_vm` | 10 / 0 | 58,743 | 74,616 | 26,071 | 74,616 | 74,560 | ĐẠT |
| `kill_vm` *(bổ sung)* | 10 / 0 | 58,669 | 87,681 | 32,891 | 87,681 | 87,670 | ĐẠT |
| `kill_host` *(bổ sung)* | 10 / 0 | 55,948 | 71,767 | 23,486 | 71,767 | 71,763 | ĐẠT |
| `link_down_reappear` | 10 / 0 | 10,044 | 12,442 | 7,683 | 12,442 | 12,385 | chỉ ghi nhận |
| `pause_vm_reappear` | 10 / 0 | 17,008 | 17,011 | 0,379 | 17,011 | 16,980 | chỉ ghi nhận |

Không có lần nào quá hạn hay lỗi đo (0 / 0 ở mọi ca). Ở bốn ca mất mạng / ngủ / bị giết, mọi giá trị đều nằm dưới cận trên đã tính ở protocol mục 8.

**Kết luận mvp N1: ĐẠT trên máy ảo bridged** (cả ba phần: xuất hiện, thoát app, mất mạng đột ngột hoặc ngủ; 10 lần hợp lệ mỗi ca). Kết luận này chỉ có giá trị trong giới hạn dưới đây; chưa thay được phép đo trên hai máy thật (mục 3.4).

**Ghi nhận** (không phải yêu cầu, không có ngưỡng)

- Thấy lại sau khi cắm lại mạng (`link_down_reappear`): trung vị 10,044 s, trong khoảng 7,683–12,442 s.
- Thấy lại sau khi VM chạy tiếp (`pause_vm_reappear`): 9/10 lần nằm sát nhau trong 17,003–17,011 s (cận dưới 16,933–16,980 s); một lần (lần 5) chỉ 0,379 s. Giá trị gần như cố định này là quan sát cần tìm hiểu; lần đo này chưa có dữ liệu để biết nó đến từ đâu (node trên VM, zeroconf, hay đồng hồ VM sau khi chạy tiếp).
- Host quảng bá cả `172.18.96.1` dù node lọc card ảo (protocol mục 2.1). Theo danh sách card trong `results.jsonl`, `172.18.96.1/20` nằm trên card “Hyper-V Virtual Ethernet Adapter”, tên Windows `vEthernet (WSL (Hyper-V firewall))`. Nguyên nhân và cách sửa ghi ở mục `### Fixed` của [CHANGELOG.md](CHANGELOG.md) (địa chỉ ưu tiên lấy theo tuyến multicast); lần đo này chạy trên mã trước khi sửa (commit `efa9d5b`), nên chưa có số đo với bản đã sửa. VM vẫn thấy host đúng hạn (`appear_host`), nhưng địa chỉ này đi vào danh sách địa chỉ máy kia sẽ thử kết nối.

**Giới hạn của lần đo này**

- Gói tin giữa host và máy ảo bridged được hypervisor chuyển ngay trong máy, không qua điểm phát Wi-Fi, router hay switch. Phép đo vì vậy không kiểm cách mạng thật xử lý multicast (Wi-Fi tiết kiệm điện, tốc độ multicast thấp, IGMP snooping, cô lập thiết bị). Trên hai máy thật qua Wi-Fi, mDNS có thể chậm hoặc mất gói nhiều hơn.
- Host và máy ảo dùng chung CPU, RAM và card mạng.
- VirtualBox chạy trên nền Hyper-V của Windows (VirtualBox hiện biểu tượng rùa ở thanh trạng thái VM), nên hiệu năng và độ đều của thời gian trong VM có thể khác khi VirtualBox chạy trực tiếp trên phần cứng; lần đo này không đo ảnh hưởng đó. Lúc cài, người dùng thấy kernel của VM báo *soft lockup*. Hai điều này không được ghi trong `results.jsonl`.
- Cắt liên kết ở hypervisor giống rút cáp ở phía máy ảo, không giống hệt rớt Wi-Fi hay đổi điểm phát.
- Tạm dừng VM là đóng băng, không phải máy ngủ thật: hệ điều hành khách không nhận thông báo ngủ, và đồng hồ sau khi chạy tiếp tùy vào hypervisor / Guest Additions.
- Chỉ một cặp hệ điều hành: Windows 11 ↔ Ubuntu 26.04. Các ca cắt mạng / tạm dừng chỉ có Windows làm bên quan sát; riêng `exit_host`, `appear_host` và `kill_host` có Linux làm bên quan sát. Cả hai phía chạy Python 3.14, chưa đo với Python 3.12.
- Các node không ghép đôi: `DeviceOnline` / `DeviceOffline` đến từ mDNS, không phụ thuộc kết nối điều khiển. Đường ưu tiên thiết bị đã ghép đôi khi danh sách đầy (protocol mục 8) không được kiểm ở đây.
- Giới hạn đã biết ở protocol mục 8 (hai lần resolve đầu đều mất gói) **chưa đo**: cần chèn mất gói có chủ đích.

### 3.4. Việc còn lại

- Đo N1 trên hai máy thật trong cùng mạng Wi-Fi, và trên mạng dây, để bỏ được giới hạn “chuyển gói trong máy” ở mục 3.3: **chưa đo**.
- Kiểm kịch bản mất gói ở hai lần resolve đầu (protocol mục 8): **chưa đo**.
- Thêm cặp hệ điều hành khác (macOS, Windows ↔ Windows) theo mvp N10: **chưa đo**.
- Tìm hiểu hai ghi nhận ở mục 3.3 (thấy lại ≈ 17 s sau khi VM chạy tiếp; địa chỉ card Hyper-V được quảng bá).

### 3.5. Cách đo lại

Script nằm ngoài repo, trong thư mục `n1-bench` cạnh thư mục repo (không commit); hướng dẫn từng bước (chuẩn bị VM, mở tường lửa tạm chỉ cho IP của VM, dọn dẹp) ở `HUONG_DAN.md` trong thư mục đó, dữ liệu thô của lần đo này ở `results/`. Trên VM chép `n1_common.py` và `n1_vm_agent.py`, cài Teleport đúng commit cần đo vào `.venv`.

```
# trong VM
python n1_vm_agent.py --bind <VM_IP> --allow-host <HOST_IP>

# trên host, trong thư mục n1-bench, dùng python của .venv trong repo
python n1_host.py preflight --vm-ip <VM_IP> --hv vbox --vm <tên VM>
python n1_host.py run --vm-ip <VM_IP> --hv vbox --vm <tên VM> --cases vm_subject,host_subject,open
python n1_host.py run --vm-ip <VM_IP> --hv vbox --vm <tên VM> --cases kill_vm,kill_host
python n1_host.py run --vm-ip <VM_IP> --hv vbox --vm <tên VM> --cases link_down,pause
python n1_host.py summarize (Get-ChildItem results\run-*\results.jsonl).FullName --write results\n1-tonghop
```

Lệnh trên host viết cho PowerShell (Python trên Windows không tự mở rộng `run-*`).

`vm_subject` gồm `appear_vm` + `exit_vm`, `host_subject` gồm `appear_host` + `exit_host`, `open` là `open_host`, `link_down` và `pause` kèm ca thấy lại tương ứng. Mỗi lượt ghi vào `results/run-<giờ UTC>/` (`results.jsonl`, `summary.md`, `summary.json`); `--repeat` (mặc định 10) và `--seed` chỉnh số lần và hạt giống. Preflight phải báo đạt mới chạy tiếp.
