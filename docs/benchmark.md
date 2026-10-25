# Benchmark

> **Vai trò:** số đo thực tế của các yêu cầu phi chức năng.
> **Nguồn duy nhất cho:** định nghĩa từng phép đo, môi trường, kết quả từng lần đo và kết luận so với ngưỡng.
> **Không chứa:** ngưỡng yêu cầu → [mvp.md](mvp.md) mục 9 (tham chiếu theo mã `mvp N1`…, không chép số). Thiết kế khám phá, TTL, chu kỳ kiểm còn sống và cận trên đã tính → [protocol.md](protocol.md) mục 8. Trạng thái giai đoạn → [ke_hoach.md](ke_hoach.md) mục 5.1.
> **Cập nhật:** 2026-10-26

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
| mvp N3 | 2026-10-26: đo sơ bộ Giai đoạn 2, hai tiến trình trên một máy Windows 11, loopback, file 64 MiB và 1 GiB (mục 4.3) | **CHƯA ĐỦ DỮ LIỆU**: phần “RSS tăng khi truyền” ĐẠT với 1 GiB và không tăng theo kích thước; 5 GB, 10 GB, lúc rảnh, ổ đích chậm, hai máy thật chưa đo (mục 4.4) |
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

## 4. mvp N3: Bộ nhớ

### 4.1. Định nghĩa phép đo

Đo bộ nhớ của **từng tiến trình node** (gửi, nhận) từ một tiến trình điều phối thứ ba, bằng `GetProcessMemoryInfo` của Windows (đọc qua `ctypes`, không dùng `psutil`). “RSS” ở đây là *working set* (`WorkingSetSize`); ghi kèm *private bytes* (`PrivateUsage`, bộ nhớ riêng đã cam kết), vì working set có cả trang dùng chung (DLL).

| Đại lượng | Cách lấy |
|---|---|
| RSS lúc bắt đầu | Working set đọc ngay trước khi điều phối ra lệnh gửi; lúc này hai node đã nối kết nối điều khiển và đã rảnh 3 s |
| RSS đỉnh | `PeakWorkingSetSize` (đỉnh do nhân ghi, không lỡ mẫu) đọc 0,5 s sau khi cả hai bên báo `TransferFinished`. Đỉnh này tính từ lúc tiến trình khởi động, nên luôn ≥ đỉnh thật trong lúc truyền; ghi kèm đỉnh nhân đọc lúc bắt đầu để biết đỉnh có rơi vào lúc truyền không |
| RSS tăng (cận trên) | RSS đỉnh − RSS lúc bắt đầu. Dùng giá trị này để kết luận |
| RSS tăng (lấy mẫu) | Working set lớn nhất trong các mẫu lấy mỗi 10 ms (thực tế khoảng 15 ms, theo nhịp hẹn giờ của Windows) từ lúc ra lệnh gửi tới 0,5 s sau khi xong, trừ RSS lúc bắt đầu; dùng để vẽ đường RSS |
| Private bytes tăng (cận trên) | `PeakPagefileUsage` sau cùng − private bytes lúc bắt đầu |
| Thông lượng (chỉ tham khảo, không phải mvp N2) | Kích thước file / (lúc bên gửi nhận `TransferFinished` − lúc bên gửi gọi `transfers.send`), cùng đồng hồ `time.perf_counter()` của tiến trình gửi. Gồm cả `offer`/`accept`, `fsync`, đọc lại và kiểm Merkle ở bên nhận |

Kết luận phần truyền: so RSS tăng (cận trên) với ngưỡng RSS tăng của mvp N3 theo cách hiểu chặt hơn (MB = 10⁶ byte); “không tăng theo kích thước” xét bằng hai cách: RSS tăng của 1 GiB so với 64 MiB, và đỉnh RSS lấy mẫu theo từng phần tư thời gian truyền 1 GiB.

### 4.2. Phương pháp

- Hai node là **hai tiến trình Python riêng**, mỗi bên một thư mục cấu hình riêng (danh tính riêng), đã tin nhau: `trusted.json` dựng sẵn bằng `TrustStore.add` như fixture `trust_each_other` của test, không qua ghép đôi. Nghe `127.0.0.1` cổng ngẫu nhiên, không mDNS. Bên gửi nối kết nối điều khiển tới bên nhận bằng `Node.connect_hint`; kết nối dữ liệu đi cùng chiều.
- Bên nhận: `Node(receive_dir=…, auto_accept=True)`; bên gửi gọi `node.transfers.send(device_id, [path])`. Cấu hình mặc định của mã, không vá hằng số nào (khác test: không đổi `connection.DRAIN_TIMEOUT`), không giới hạn tốc độ.
- File nguồn là dữ liệu ngẫu nhiên (`os.urandom`), tạo một lần mỗi lượt, đặt cùng ổ với thư mục nhận. Mỗi lần đo bật hai tiến trình mới, truyền một file, so SHA-256 file nhận với file nguồn, xóa file nhận, tắt hai node (`Node.close()`).
- Lặp 10 vòng; mỗi vòng đo cả 64 MiB và 1 GiB, đổi thứ tự xen kẽ giữa các vòng. Mọi lần chờ có giới hạn (một lần truyền tối đa 600 s, cả lượt tối đa 3 giờ); kết thúc thì tắt mọi tiến trình và xóa thư mục làm việc (file nguồn, thư mục nhận, thư mục cấu hình).

### 4.3. Lần đo 2026-10-26: đo sơ bộ Giai đoạn 2 (P2-6b), loopback trên một máy

Lượt `run-20261025T182834Z` (giờ UTC). Mã đo: nhánh `feat/transfer-manager`, commit `5c2ed74` cộng thay đổi chưa commit của P2-6b (`src/teleport/app/node.py` sửa, `src/teleport/app/transfers.py` mới); digest mã nguồn `src/teleport` (SHA-256 trên đường dẫn + nội dung mọi file `.py`) `a7143d814e2b7637582f1c7cd61f7ca8f91ae62647e4e9526675694e030624d4`. Script: `n3_bench.py`, `n3_node.py` (mục 4.5).

**Môi trường**

| | |
|---|---|
| Hệ điều hành | Windows 11, bản 10.0.26300 |
| Python | 3.14.5 (64 bit), `.venv` của repo |
| Teleport | `__version__` 0.0.1 (mã nạp từ `src/` của worktree, đã kiểm trong từng tiến trình) |
| cryptography / zeroconf / ifaddr | 50.0.2 / 0.151.5 / 0.2.0 (zeroconf không dùng: tắt mDNS) |
| CPU | Intel Core i7-12800H, 20 luồng |
| RAM | 15,7 GiB; còn trống lúc bắt đầu lượt 3,4 GiB (máy đang chạy các ứng dụng khác) |
| Ổ đĩa | NVMe SSD SSSTC CL1-3D512-Q11 512 GB; file nguồn và thư mục nhận cùng ổ |
| Mạng | Loopback `127.0.0.1` (không qua card mạng) |

**Kết quả** (10 lần hợp lệ mỗi cỡ, 0 lần hỏng; mọi lần đều `done` ở cả hai bên, SHA-256 khớp, không sót file trong thư mục nhận, hai tiến trình thoát mã 0). Đơn vị MiB; mỗi ô là trung vị / p95 / min / max.

| Cỡ file | Tiến trình | RSS lúc bắt đầu | RSS đỉnh (nhân) | RSS tăng, cận trên | RSS tăng, lấy mẫu | Private bytes lúc bắt đầu | Private bytes tăng, cận trên |
|---|---|---|---|---|---|---|---|
| 64 MiB | gửi | 49,7 / 50,1 / 49,1 / 50,1 | 53,5 / 54,0 / 53,2 / 54,0 | 3,9 / 4,2 / 3,2 / 4,2 | 3,9 / 4,2 / 3,2 / 4,2 | 31,5 / 32,1 / 30,9 / 32,1 | 3,7 / 4,0 / 2,7 / 4,0 |
| 64 MiB | nhận | 49,4 / 50,5 / 49,2 / 50,5 | 60,8 / 61,4 / 57,0 / 61,4 | 11,2 / 12,0 / 6,5 / 12,0 | 8,7 / 9,4 / 3,3 / 9,4 | 31,6 / 32,2 / 31,3 / 32,2 | 11,6 / 12,3 / 6,8 / 12,3 |
| 1 GiB | gửi | 49,8 / 50,3 / 49,3 / 50,3 | 53,6 / 54,0 / 53,0 / 54,0 | 3,8 / 4,2 / 3,5 / 4,2 | 3,8 / 4,2 / 3,5 / 4,2 | 31,6 / 32,6 / 31,2 / 32,6 | 3,6 / 3,9 / 3,0 / 3,9 |
| 1 GiB | nhận | 49,3 / 49,8 / 49,1 / 49,8 | 61,3 / 61,9 / 60,3 / 61,9 | 11,7 / 12,6 / 10,8 / 12,6 | 10,3 / 11,7 / 8,8 / 11,7 | 31,6 / 32,2 / 31,3 / 32,2 | 12,1 / 13,1 / 10,5 / 13,1 |

Ở cả 40 lần (2 cỡ × 2 tiến trình × 10), đỉnh nhân đọc lúc bắt đầu bằng đúng RSS lúc bắt đầu, nên “RSS đỉnh (nhân)” chính là đỉnh trong lúc truyền, không lẫn đỉnh lúc khởi động.

**Đường RSS khi truyền 1 GiB** (RSS lớn nhất trong từng phần tư thời gian truyền, MiB, theo mẫu)

| Lần | Thời gian (s) | Gửi: Q1 / Q2 / Q3 / Q4 | Nhận: Q1 / Q2 / Q3 / Q4 |
|---|---|---|---|
| 2 | 15,6 | 53,2 / 53,2 / 53,2 / 53,2 | 58,3 / 58,6 / 58,5 / 58,5 |
| 3 | 10,5 | 53,6 / 53,6 / 53,6 / 53,6 | 58,2 / 58,3 / 60,0 / 58,2 |
| 6 | 8,8 | 53,9 / 53,9 / 53,9 / 53,9 | 57,7 / 59,8 / 58,0 / 57,1 |
| 7 | 10,3 | 53,7 / 53,7 / 53,7 / 53,7 | 58,7 / 58,6 / 60,6 / 60,5 |
| 10 | 12,9 | 53,4 / 53,4 / 53,4 / 53,5 | 58,3 / 57,4 / 59,6 / 58,8 |
| 11 | 8,2 | 54,0 / 54,0 / 54,0 / 54,0 | 59,3 / 58,6 / 59,3 / 58,1 |
| 14 | 11,7 | 53,7 / 53,7 / 53,7 / 53,7 | 59,1 / 58,4 / 58,5 / 58,4 |
| 15 | 11,4 | 53,0 / 53,0 / 53,0 / 53,0 | 58,1 / 59,4 / 55,5 / 58,5 |
| 18 | 7,4 | 54,0 / 54,0 / 54,0 / 54,0 | 60,2 / 59,0 / 58,7 / 58,7 |
| 19 | 15,0 | 53,2 / 53,2 / 53,2 / 53,2 | 59,7 / 58,7 / 60,9 / 60,5 |

**Thông lượng loopback** (chỉ tham khảo, không phải mvp N2; trung vị / p95 / min / max)

| Cỡ file | MB/s (10⁶ byte/s) | Thời gian (s) |
|---|---|---|
| 64 MiB | 133,6 / 162,9 / 18,2 / 162,9 | 0,50 / 3,70 / 0,41 / 3,70 |
| 1 GiB | 98,1 / 145,8 / 68,7 / 145,8 | 10,97 / 15,63 / 7,37 / 15,63 |

Lần chậm nhất của 64 MiB (18,2 MB/s, 3,70 s) là lần đo đầu tiên của lượt, ngay sau khi tạo file nguồn; lần đo này không tìm hiểu nguyên nhân. Thông lượng dao động mạnh vì hai node, file nguồn và thư mục nhận dùng chung CPU và ổ, và máy đang chạy các ứng dụng khác.

**Kết luận**

- RSS tăng khi truyền 1 GiB: cận trên lớn nhất 12,6 MiB (tiến trình nhận), 4,2 MiB (tiến trình gửi), dưới ngưỡng của mvp N3 ở cả 10 lần, cả hai tiến trình: **ĐẠT** (tiêu chí sơ bộ của P2-6b).
- Không tăng theo kích thước: RSS tăng của 1 GiB so với 64 MiB khác nhau trong khoảng dao động giữa các lần (gửi: trung vị 3,8 so với 3,9 MiB; nhận: 11,7 so với 11,2 MiB, cỡ 64 MiB tự dao động 6,5–12,0 MiB); trong lúc truyền 1 GiB, đường RSS phẳng (bảng trên, không phần tư nào tăng dần): **ĐẠT** với hai cỡ đã đo.
- **mvp N3 nói chung: CHƯA ĐỦ DỮ LIỆU**: mvp N3 đòi cả các cỡ lớn hơn và phần lúc rảnh (mục 4.4).

**Ghi nhận** (không phải kết luận)

- Lúc rảnh (đã nối kết nối điều khiển, chưa truyền), working set mỗi tiến trình node khoảng 49–50,5 MiB, private bytes khoảng 31–32,6 MiB. Đây là node không có giao diện và **không bật mDNS**, nên không dùng để kết luận phần lúc rảnh của mvp N3.
- Tiến trình nhận tăng nhiều hơn tiến trình gửi (khoảng 11–12 MiB so với 4 MiB). Lần đo này không tách xem phần tăng đó thuộc phần nào của mã.

**Giới hạn của lần đo này**

- Hai tiến trình trên cùng một máy, qua loopback: không qua card mạng, không có mất gói hay trễ thật; khi mạng chậm hơn đĩa, hàng chờ ở hai bên có thể đầy theo cách khác.
- Ổ đích là NVMe SSD nhanh và dùng chung với ổ nguồn; ca “ổ đích chậm” (bên nhận phải chờ đĩa) chưa đo.
- File nguồn vừa được tạo nên nằm trong bộ đệm file của Windows; bộ đệm file của hệ điều hành không tính vào working set của tiến trình, và phép đo này không đo nó.
- Chỉ một file mỗi lượt, một lượt mỗi lần; không đo nhiều file hay nhiều lượt cùng lúc. Không giới hạn tốc độ.
- Chỉ Windows 11 và Python 3.14; chưa đo Linux, macOS, Python 3.12.

### 4.4. Việc còn lại

- File 5 GB và 10 GB theo mvp N3: **chưa đo** (cần khoảng 2 lần dung lượng file trống trên ổ; script có sẵn cỡ `5GiB`, cần thêm cỡ 10 GiB).
- Phần lúc rảnh của mvp N3 (RAM và CPU của app hoàn chỉnh có mDNS, sau này có giao diện): **chưa đo**. Đo CPU cần tránh bẫy lõi tiết kiệm điện của Windows (EcoQoS).
- Ổ đích chậm mà RAM không tăng (điều kiện xong Giai đoạn 2, ke_hoach mục 5.2): **chưa đo**; cần ổ chậm thật (USB, thẻ nhớ) hoặc giới hạn tốc độ ghi.
- Hai máy thật trong mạng LAN, và hệ điều hành khác: **chưa đo**.

### 4.5. Cách đo lại

Script nằm ngoài repo (không commit): `n3_bench.py` (điều phối, đọc bộ nhớ, tóm tắt) và `n3_node.py` (một node trong tiến trình riêng, điều khiển qua stdin/stdout bằng JSON Lines; stdin đọc bằng `os.read` trong thread). Chỉ chạy trên Windows. Trong PowerShell, ở thư mục chứa hai script:

```
$env:PYTHONPATH = "<thư mục repo hoặc worktree>\src"
& "<repo>\.venv\Scripts\python.exe" n3_bench.py --repeat 10 --sizes 64MiB,1GiB
```

`--sizes` nhận `64MiB`, `256MiB`, `1GiB`, `5GiB`; `--settle` (mặc định 3 s) là thời gian rảnh trước khi gửi, `--sample-ms` (mặc định 10) là nhịp lấy mẫu, `--work` là thư mục làm việc (mặc định `work/` cạnh script; bị xóa khi xong, trừ khi có `--keep`). Mỗi lượt ghi vào `results/run-<giờ UTC>/`: `results.jsonl` (môi trường, cấu hình, từng lần đo kèm mọi sự kiện của hai node), `samples-<lần>-<cỡ>.csv` (đường RSS và private bytes theo thời gian), `summary.md`, `summary.json`, và stderr của từng tiến trình node.
