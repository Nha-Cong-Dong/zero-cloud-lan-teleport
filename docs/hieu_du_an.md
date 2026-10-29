# Hiểu dự án qua một ví dụ

Ghi chú tôi viết để tự hiểu dự án bằng lời đơn giản. Bạn đọc thấy chỗ nào chưa đúng hay chưa rõ thì cứ nhận xét và hỏi lại tôi. Đặc tả chính xác nằm ở [protocol.md](protocol.md) (ghép đôi: mục 5; truyền file: mục 3, 4) và [architecture.md](architecture.md).

**Ví dụ có 2 máy A và máy B. Giả sử máy A muốn gửi 1 file video 10 GB cho máy B.**

## 1. Luồng ghép đôi

- **Lý do phải ghép đôi:**
  - Hai máy A và B không biết nhau.
  - Nếu không ghép đôi mà cứ thế gửi file thì máy lạ trong cùng Wi-Fi đều gửi bừa được, hoặc giả làm máy B để lấy trộm file của máy A.
  - → Vì vậy 2 máy A và B cần làm quen và ghi nhớ nhau.
- **Các bước ghép đôi:**
  - **Bước 0:** lần đầu chạy, mỗi máy tự tạo một cặp khóa: khóa công khai (in trong chứng chỉ) và khóa riêng.
    - Khóa riêng dùng để tạo chữ ký, chỉ nằm trong máy, không bao giờ gửi đi.
    - Khóa công khai chỉ kiểm tra được chữ ký, không tạo được; ai xem cũng được.
    - Đảm bảo: từ khóa công khai không thể chế ra khóa riêng.
  - **Bước 1:** máy A gọi máy B.
    - `Máy B: teleport listen` → máy B sẵn sàng nhận làm quen với 1 máy lạ.
    - `Máy A: teleport pair 192.168.1.5` → máy A gọi tới đúng địa chỉ của máy B.
    - Cuộc gọi được mã hóa, nghĩa là máy C chỉ nghe trộm thì không đọc được; máy C chen vào giữa thì bị lộ ở bước so mã (bước 4).
    - Sau đó 2 máy giới thiệu bản thân, **tự xưng tên và mã máy**, nhưng **chưa chứng minh được**.
    - Máy **A đưa ra yêu cầu ghép đôi kèm căn cước (chứng chỉ)** của mình. Máy **B kiểm tra** căn cước có đúng mẫu không, có khớp với mã máy A vừa tự xưng không.
  - **Bước 2:** mỗi máy góp 1 số ngẫu nhiên; máy B gửi số của mình theo kiểu "phong bì niêm phong".
    - **Mã 6 số được tính từ chứng chỉ của 2 máy + 2 số ngẫu nhiên này.**
    - Mỗi máy góp một số ngẫu nhiên để tạo mã 6 số, và không máy nào được chọn số sau khi đã thấy số của máy kia. Nhờ số ngẫu nhiên mới mỗi lần, kẻ thứ 3 không thể tính trước trong 1 triệu khả năng để tìm ra mã trùng.
    - Cách làm:
      - `Máy B: "Đây là phong bì của tôi"` → **gửi kết quả băm** SHA-256 của số 1309, giấu số thật.
      - `Máy A: gửi thẳng số 0912` (không bỏ vào phong bì; đường truyền vẫn được mã hóa).
      - `Máy B: hiện mã lên màn hình, rồi mới mở phong bì: "Số của tôi là 1309"` → **máy A băm số 1309 để kiểm tra** có khớp phong bì không.
    - → Nhờ vậy máy A không biết trước số của máy B, còn máy B không đổi được số sau khi đã gửi phong bì, nên không ai gian lận được.
  - **Bước 3:** ký tên chứng minh chứng chỉ là của mình.
    - **Máy A ký cùng lúc gửi số 0912** ở bước 2. Tờ giấy được ký có chứng chỉ của 2 máy, số của máy A và phong bì của máy B trong phiên này.
    - `Máy A: dùng khóa riêng ký, gửi chữ ký đi`
    - `Máy B: dùng khóa công khai (trong chứng chỉ của máy A) kiểm tra; đúng thì máy A đúng là chủ của chứng chỉ`
    - Kẻ thứ 3 có thể có bản chép chứng chỉ nhưng không có khóa riêng nên không ký được.
    - Đảm bảo: chữ ký cũ không dùng lại được ở phiên khác (mỗi phiên có phong bì mới).
  - **Bước 4:** người dùng ở hai máy tự so mã bằng mắt.
    - Mỗi máy tự tính mã 6 số từ những gì nó nhìn thấy (2 chứng chỉ + 2 số ngẫu nhiên).
      - `Máy A thấy:  CCCD máy A + CCCD máy B + số 0912 + số 1309  →  băm SHA-256  →  123456`
      - `Máy B thấy:  CCCD máy A + CCCD máy B + số 0912 + số 1309  →  băm SHA-256  →  123456`
    - **Nếu có kẻ thứ 3 là máy C đứng giữa thì:**
      - `Máy A thấy:  CCCD máy A + CCCD máy C + ...  →  băm SHA-256  →  123456`
      - `Máy B thấy:  CCCD máy C + CCCD máy B + ...  →  băm SHA-256  →  654321`
      - → mã không khớp, không ghép đôi.
    - 2 mã khớp thì bấm xác nhận ở cả 2 máy để hoàn tất ghép đôi.
  - **Bước 5:** ghi sổ máy quen.
    - Mỗi máy lưu chứng chỉ của máy kia vào file `trusted.json`. Lần sau kết nối, 2 máy tự kiểm tra căn cước của nhau mà không phải hỏi lại nữa.

## 2. Gửi file

File lớn được chia nhỏ ra gửi từng phần (khối), kiểm tra từng phần rồi ghép lại ở máy nhận.

- **Bước 1:** máy A hỏi máy B.
  - `Máy A: Mình gửi video 10 GB được không?`
  - `Máy B: hỏi người dùng có nhận hay không`
- **Bước 2:** gửi từng phần, kiểm tra từng phần.
  - `Máy A: chia file thành phần 1, phần 2, phần 3, ..., mỗi phần kèm mã băm`
  - `Máy B: phần nào đúng thì ghi vào thư mục tạm ẩn trên ổ đĩa, phần nào sai thì báo gửi lại`
- **Bước 3:** ghi sổ trong lúc nhận.
  - Ghi phần đó xuống ổ đĩa xong hẳn rồi mới ghi vào sổ (file `.state`) "đã có phần này". → Đang gửi bị ngắt, kể cả tắt máy, thì không phải gửi lại toàn bộ, chỉ gửi phần còn thiếu.
- **Bước 4:** kiểm tra lần cuối.
  - `Nhận đủ tất cả các phần` → `đọc lại và kiểm lại toàn bộ` → đúng → đổi về tên gốc (ví dụ `tung.mp4`).
  - Trong lúc nhận, file nằm trong thư mục tạm ẩn `.teleport/`, nên người dùng không thấy file dở dang.
  - Nếu đã có sẵn `tung.mp4` thì tự đặt tên `tung (1).mp4`, không bao giờ ghi đè file cũ.
