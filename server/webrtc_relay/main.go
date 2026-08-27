// relay: 低遅延WebRTC中継サーバー (SFU) — 複数ラズパイ対応版
//
// 構成:
//   複数のRaspberry Pi (GStreamer/webrtcbin) --WebRTC(H.264)--> [relay/SFU] --WebRTC--> 複数ブラウザ
//
// 多重化:
//   - 各PiはWebSocket接続時に自分のID(4文字程度)を申告する。
//   - SFUは ID -> ストリーム で管理し、ビュアーは見たいIDを指定して接続する。
//
// カメラ切替 (camChange):
//   - ビュアーから送られた camChange を、SFUが該当IDのPiのWebSocketへ転送する。
//   - 切替直後はそのPiへPLIを送り、ビュアーが素早くキーフレームを得られるようにする。
//
// シグナリングの役割分担:
//   - publisher(Pi)  は offerer (webrtcbinがオファーを生成)
//   - viewer(ブラウザ) は answerer (サーバーがトラックを乗せたオファーを生成)
package main

import (
	"encoding/json"
	"flag"
	"log"
	"net/http"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/pion/rtcp"
	"github.com/pion/webrtc/v4"
)

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true }, // LAN内・管理端末前提
}

var (
	api     *webrtc.API
	mu      sync.Mutex
	streams = map[string]*stream{} // PiのID -> ストリーム
)

// stream は1台のPi(publisher)に対応する配信状態。
type stream struct {
	id     string
	track  *webrtc.TrackLocalStaticRTP // viewerへ配るファンアウト用トラック(安定)
	pubPC  *webrtc.PeerConnection
	pub    *client // camChange転送用のpublisher WebSocket
	ssrc   uint32
}

// client はWebSocket接続(送信を直列化)。
type client struct {
	conn *websocket.Conn
	wmu  sync.Mutex
}

func (c *client) send(m sigMsg) {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	_ = c.conn.WriteJSON(m)
}

// シグナリングメッセージ (WebSocket / JSON)
type sigMsg struct {
	Type      string                     `json:"type"`                // hello/offer/answer/candidate/camChange/error
	Role      string                     `json:"role,omitempty"`      // publisher/viewer
	ID        string                     `json:"id,omitempty"`        // 号機ID (KK0N)
	SDP       *webrtc.SessionDescription `json:"sdp,omitempty"`       // offer/answer
	Candidate *webrtc.ICECandidateInit   `json:"candidate,omitempty"` // ICE
	Cam       *int                       `json:"cam,omitempty"`       // camChangeのカメラ番号(0=screen)
	Message   string                     `json:"message,omitempty"`   // error詳細
}

func main() {
	addr := flag.String("addr", ":8080", "HTTP/WS待受アドレス")
	webDir := flag.String("web", "../web", "Webクライアントのディレクトリ")
	flag.Parse()

	m := &webrtc.MediaEngine{}
	if err := m.RegisterDefaultCodecs(); err != nil {
		log.Fatalf("RegisterDefaultCodecs: %v", err)
	}
	api = webrtc.NewAPI(webrtc.WithMediaEngine(m))

	http.HandleFunc("/ws", wsHandler)
	http.HandleFunc("/pis", pisHandler) // 接続中のPi一覧(UI/デバッグ用)
	http.Handle("/", http.FileServer(http.Dir(*webDir)))

	log.Printf("relay listening on %s  (open http://localhost%s/ )", *addr, *addr)
	log.Fatal(http.ListenAndServe(*addr, nil))
}

// pisHandler: 接続中のPi ID一覧をJSONで返す。
func pisHandler(w http.ResponseWriter, r *http.Request) {
	mu.Lock()
	ids := make([]string, 0, len(streams))
	for id := range streams {
		ids = append(ids, id)
	}
	mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"pis": ids})
}

func wsHandler(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("ws upgrade: %v", err)
		return
	}
	defer conn.Close()

	cl := &client{conn: conn}
	var (
		pc         *webrtc.PeerConnection
		role       string
		myID       string // publisher: 自分のID / viewer: 視聴対象ID
		pending    []webrtc.ICECandidateInit
		haveRemote bool
	)

	defer func() {
		if pc != nil {
			_ = pc.Close()
		}
		if role == "publisher" {
			mu.Lock()
			if s := streams[myID]; s != nil && s.pubPC == pc {
				delete(streams, myID)
			}
			mu.Unlock()
			log.Printf("publisher[%s] disconnected", myID)
		}
	}()

	for {
		var msg sigMsg
		if err := conn.ReadJSON(&msg); err != nil {
			return
		}

		switch msg.Type {
		case "hello":
			role = msg.Role
			switch role {
			case "publisher":
				// 号機 ID は KK0N に統一。既定値で代替すると号機1の枠を
				// 奪う事故になり得るので、id 無しは受け付けない。
				if msg.ID == "" {
					cl.send(sigMsg{Type: "error", Message: "publisher must send id (KK0N)"})
					continue
				}
				myID = msg.ID
				pc = setupPublisher(cl, myID)
				log.Printf("publisher[%s] connected", myID)
			case "viewer":
				myID = msg.ID
				var ok bool
				pc, ok = setupViewer(cl, myID)
				if !ok {
					cl.send(sigMsg{Type: "error", Message: "no publisher for id: " + myID})
					continue
				}
				log.Printf("viewer connected -> watching[%s]", myID)
			default:
				cl.send(sigMsg{Type: "error", Message: "unknown role"})
			}

		case "offer": // publisher(webrtcbin)からのオファー
			if pc == nil || msg.SDP == nil {
				continue
			}
			if err := pc.SetRemoteDescription(*msg.SDP); err != nil {
				log.Printf("SetRemoteDescription(offer): %v", err)
				continue
			}
			haveRemote = true
			drainPending(pc, &pending)
			answer, err := pc.CreateAnswer(nil)
			if err != nil {
				log.Printf("CreateAnswer: %v", err)
				continue
			}
			if err := pc.SetLocalDescription(answer); err != nil {
				log.Printf("SetLocalDescription(answer): %v", err)
				continue
			}
			cl.send(sigMsg{Type: "answer", SDP: pc.LocalDescription()})

		case "answer": // viewer(ブラウザ)からのアンサー
			if pc == nil || msg.SDP == nil {
				continue
			}
			if err := pc.SetRemoteDescription(*msg.SDP); err != nil {
				log.Printf("SetRemoteDescription(answer): %v", err)
				continue
			}
			haveRemote = true
			drainPending(pc, &pending)

		case "candidate":
			if pc == nil || msg.Candidate == nil {
				continue
			}
			if !haveRemote {
				pending = append(pending, *msg.Candidate)
				continue
			}
			if err := pc.AddICECandidate(*msg.Candidate); err != nil {
				log.Printf("AddICECandidate: %v", err)
			}

		case "camChange": // viewer -> 該当IDのPiへ転送
			if role != "viewer" || msg.Cam == nil {
				continue
			}
			forwardCamChange(myID, msg.Cam)
		}
	}
}

func drainPending(pc *webrtc.PeerConnection, pending *[]webrtc.ICECandidateInit) {
	for _, c := range *pending {
		if err := pc.AddICECandidate(c); err != nil {
			log.Printf("AddICECandidate(pending): %v", err)
		}
	}
	*pending = nil
}

// setupPublisher: Piからの受信用PeerConnection。受信トラックを安定したファンアウト用
// トラックへ中継し、ストリームをIDで登録する。
func setupPublisher(cl *client, id string) *webrtc.PeerConnection {
	pc, err := api.NewPeerConnection(webrtc.Configuration{}) // LAN内なのでSTUN/TURN不要
	if err != nil {
		log.Fatalf("NewPeerConnection(publisher): %v", err)
	}

	vt, err := webrtc.NewTrackLocalStaticRTP(
		webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeH264, ClockRate: 90000},
		"video", "pi-"+id,
	)
	if err != nil {
		log.Fatalf("NewTrackLocalStaticRTP: %v", err)
	}

	s := &stream{id: id, track: vt, pubPC: pc, pub: cl}
	mu.Lock()
	if old := streams[id]; old != nil && old.pubPC != nil {
		_ = old.pubPC.Close() // 同一IDの古い接続は閉じる
	}
	streams[id] = s
	mu.Unlock()

	pc.OnICECandidate(func(c *webrtc.ICECandidate) {
		if c == nil {
			return
		}
		init := c.ToJSON()
		cl.send(sigMsg{Type: "candidate", Candidate: &init})
	})
	pc.OnConnectionStateChange(func(st webrtc.PeerConnectionState) {
		log.Printf("publisher[%s] PC state: %s", id, st)
	})

	pc.OnTrack(func(tr *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		mu.Lock()
		s.ssrc = uint32(tr.SSRC())
		mu.Unlock()
		log.Printf("publisher[%s] track: %s ssrc=%d", id, tr.Codec().MimeType, tr.SSRC())

		// 保険の定期PLI
		go func() {
			t := time.NewTicker(2 * time.Second)
			defer t.Stop()
			for range t.C {
				if err := pc.WriteRTCP([]rtcp.Packet{
					&rtcp.PictureLossIndication{MediaSSRC: uint32(tr.SSRC())},
				}); err != nil {
					return
				}
			}
		}()

		buf := make([]byte, 1500)
		for {
			n, _, readErr := tr.Read(buf)
			if readErr != nil {
				return
			}
			if _, werr := vt.Write(buf[:n]); werr != nil {
				return
			}
		}
	})

	return pc
}

// setupViewer: 指定IDのストリームを乗せたブラウザ向け送信PeerConnection。
func setupViewer(cl *client, id string) (*webrtc.PeerConnection, bool) {
	mu.Lock()
	s := streams[id]
	mu.Unlock()
	if s == nil || s.track == nil {
		return nil, false
	}

	pc, err := api.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		log.Fatalf("NewPeerConnection(viewer): %v", err)
	}

	pc.OnICECandidate(func(c *webrtc.ICECandidate) {
		if c == nil {
			return
		}
		init := c.ToJSON()
		cl.send(sigMsg{Type: "candidate", Candidate: &init})
	})
	pc.OnConnectionStateChange(func(st webrtc.PeerConnectionState) {
		log.Printf("viewer[%s] PC state: %s", id, st)
		if st == webrtc.PeerConnectionStateFailed || st == webrtc.PeerConnectionStateClosed {
			_ = pc.Close()
		}
	})

	sender, err := pc.AddTrack(s.track)
	if err != nil {
		log.Printf("AddTrack(viewer): %v", err)
		return pc, true
	}
	go func() {
		b := make([]byte, 1500)
		for {
			if _, _, e := sender.Read(b); e != nil {
				return
			}
		}
	}()

	offer, err := pc.CreateOffer(nil)
	if err != nil {
		log.Printf("CreateOffer(viewer): %v", err)
		return pc, true
	}
	if err := pc.SetLocalDescription(offer); err != nil {
		log.Printf("SetLocalDescription(viewer): %v", err)
		return pc, true
	}
	cl.send(sigMsg{Type: "offer", SDP: pc.LocalDescription()})

	requestKeyframe(s)
	return pc, true
}

// forwardCamChange: ビュアーのcamChangeを該当IDのPiへ転送し、切替が映るよう
// キーフレームを促す。
func forwardCamChange(id string, cam *int) {
	mu.Lock()
	s := streams[id]
	mu.Unlock()
	if s == nil || s.pub == nil {
		return
	}
	log.Printf("camChange[%s] -> cam %d", id, *cam)
	s.pub.send(sigMsg{Type: "camChange", Cam: cam})
	requestKeyframe(s)
}

func requestKeyframe(s *stream) {
	mu.Lock()
	pc, ssrc := s.pubPC, s.ssrc
	mu.Unlock()
	if pc == nil {
		return
	}
	_ = pc.WriteRTCP([]rtcp.Packet{&rtcp.PictureLossIndication{MediaSSRC: ssrc}})
}
