// relay: 低遅延WebRTC中継サーバー (SFU)
//
// 構成:
//   Raspberry Pi (GStreamer/webrtcbin) --WebRTC(H.264)--> [relay/SFU] --WebRTC--> ブラウザ(複数)
//
// 役割:
//   - publisher (Pi) から1本のH.264トラックを受信
//   - そのRTPを再エンコードせず複数のviewer(ブラウザ)へファンアウト = 低遅延・低負荷
//   - WebSocketによるシグナリングと、視聴用Webクライアントの静的配信を同梱
//
// シグナリングの役割分担:
//   - publisher は offerer (GStreamer webrtcbin がオファーを生成)
//   - viewer    は answerer (サーバーがトラックを乗せたオファーを生成)
package main

import (
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

// 全体で共有する配信状態。publisherは1本のみ想定。
var (
	mu            sync.Mutex
	api           *webrtc.API
	videoTrack    *webrtc.TrackLocalStaticRTP // viewerへ配るファンアウト用トラック
	publisherPC   *webrtc.PeerConnection
	publisherSSRC uint32
)

// シグナリングメッセージ (WebSocket / JSON)
type sigMsg struct {
	Type      string                     `json:"type"`            // hello/offer/answer/candidate/error/ready
	Role      string                     `json:"role,omitempty"`  // publisher/viewer (helloのみ)
	SDP       *webrtc.SessionDescription `json:"sdp,omitempty"`   // offer/answer
	Candidate *webrtc.ICECandidateInit   `json:"candidate,omitempty"`
	Message   string                     `json:"message,omitempty"` // error詳細
}

func main() {
	addr := flag.String("addr", ":8080", "HTTP/WS待受アドレス")
	webDir := flag.String("web", "../web", "Webクライアントのディレクトリ")
	flag.Parse()

	// MediaEngineにH.264を登録 (ブラウザもPi(HW)もH.264が最も相性が良い)
	m := &webrtc.MediaEngine{}
	if err := m.RegisterDefaultCodecs(); err != nil {
		log.Fatalf("RegisterDefaultCodecs: %v", err)
	}
	api = webrtc.NewAPI(webrtc.WithMediaEngine(m))

	http.HandleFunc("/ws", wsHandler)
	http.Handle("/", http.FileServer(http.Dir(*webDir)))

	log.Printf("relay listening on %s  (open http://localhost%s/ )", *addr, *addr)
	log.Fatal(http.ListenAndServe(*addr, nil))
}

func wsHandler(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("ws upgrade: %v", err)
		return
	}
	defer conn.Close()

	var (
		pc       *webrtc.PeerConnection
		role     string
		writeMu  sync.Mutex
		pending  []webrtc.ICECandidateInit // remoteDescription確定前のcandidateを退避
		haveRemote bool
	)

	send := func(m sigMsg) {
		writeMu.Lock()
		defer writeMu.Unlock()
		_ = conn.WriteJSON(m)
	}

	defer func() {
		if pc != nil {
			_ = pc.Close()
		}
		if role == "publisher" {
			mu.Lock()
			if publisherPC == pc {
				publisherPC = nil
				videoTrack = nil
			}
			mu.Unlock()
			log.Printf("publisher disconnected")
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
				pc = setupPublisher(send)
				log.Printf("publisher connected")
			case "viewer":
				mu.Lock()
				ready := videoTrack != nil
				mu.Unlock()
				if !ready {
					send(sigMsg{Type: "error", Message: "no publisher yet"})
					continue
				}
				pc = setupViewer(send)
				log.Printf("viewer connected")
			default:
				send(sigMsg{Type: "error", Message: "unknown role"})
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
			send(sigMsg{Type: "answer", SDP: pc.LocalDescription()})

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

// setupPublisher: Piからの受信用PeerConnection。受信トラックをファンアウト用へ中継する。
func setupPublisher(send func(sigMsg)) *webrtc.PeerConnection {
	pc, err := api.NewPeerConnection(webrtc.Configuration{}) // LAN内なのでSTUN/TURN不要
	if err != nil {
		log.Fatalf("NewPeerConnection(publisher): %v", err)
	}

	pc.OnICECandidate(func(c *webrtc.ICECandidate) {
		if c == nil {
			return
		}
		init := c.ToJSON()
		send(sigMsg{Type: "candidate", Candidate: &init})
	})

	pc.OnConnectionStateChange(func(s webrtc.PeerConnectionState) {
		log.Printf("publisher PC state: %s", s)
	})

	// viewerへ配るファンアウト用トラック (H.264)。TrackLocalStaticRTPは
	// 複数のPeerConnectionにAddTrackでき、書き込んだRTPをPT/SSRCを各接続向けに
	// 書き換えて配ってくれる。
	vt, err := webrtc.NewTrackLocalStaticRTP(
		webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeH264, ClockRate: 90000},
		"video", "pi-camera",
	)
	if err != nil {
		log.Fatalf("NewTrackLocalStaticRTP: %v", err)
	}

	mu.Lock()
	videoTrack = vt
	publisherPC = pc
	mu.Unlock()

	pc.OnTrack(func(tr *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		mu.Lock()
		publisherSSRC = uint32(tr.SSRC())
		mu.Unlock()
		log.Printf("publisher track: %s ssrc=%d", tr.Codec().MimeType, tr.SSRC())

		// 定期PLIで全viewerがキーフレームを取りこぼさないようにする保険
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

// setupViewer: ブラウザ向け送信PeerConnection。ファンアウト用トラックを乗せて
// サーバー側からオファーを作る。
func setupViewer(send func(sigMsg)) *webrtc.PeerConnection {
	pc, err := api.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		log.Fatalf("NewPeerConnection(viewer): %v", err)
	}

	pc.OnICECandidate(func(c *webrtc.ICECandidate) {
		if c == nil {
			return
		}
		init := c.ToJSON()
		send(sigMsg{Type: "candidate", Candidate: &init})
	})

	pc.OnConnectionStateChange(func(s webrtc.PeerConnectionState) {
		log.Printf("viewer PC state: %s", s)
		if s == webrtc.PeerConnectionStateFailed || s == webrtc.PeerConnectionStateClosed {
			_ = pc.Close()
		}
	})

	mu.Lock()
	vt := videoTrack
	mu.Unlock()

	sender, err := pc.AddTrack(vt)
	if err != nil {
		log.Printf("AddTrack(viewer): %v", err)
		return pc
	}
	// RTCP(受信側からのPLI/NACK等)を読み捨てて詰まりを防ぐ
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
		return pc
	}
	if err := pc.SetLocalDescription(offer); err != nil {
		log.Printf("SetLocalDescription(viewer): %v", err)
		return pc
	}
	send(sigMsg{Type: "offer", SDP: pc.LocalDescription()})

	// 新規viewerのために即キーフレーム要求
	requestKeyframe()
	return pc
}

func requestKeyframe() {
	mu.Lock()
	ppc, ssrc := publisherPC, publisherSSRC
	mu.Unlock()
	if ppc == nil {
		return
	}
	_ = ppc.WriteRTCP([]rtcp.Packet{&rtcp.PictureLossIndication{MediaSSRC: ssrc}})
}
