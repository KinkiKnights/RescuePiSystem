// headless-viewer: ブラウザ無しで配信経路を検証するためのテスト用視聴クライアント。
//
// relayへviewerとして接続し、受信したH.264トラックを
//   - RTPパケット数/バイト数を集計
//   - 生H.264 (Annex-B) をファイルに保存
// する。保存ファイルをffprobeで確認すれば、Pi(役)→relay→viewer の
// エンドツーエンドで実映像が流れていることを検証できる。
package main

import (
	"flag"
	"log"
	"net/url"
	"os"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/pion/rtp/codecs"
	"github.com/pion/webrtc/v4"
)

type sigMsg struct {
	Type      string                     `json:"type"`
	Role      string                     `json:"role,omitempty"`
	ID        string                     `json:"id,omitempty"`
	SDP       *webrtc.SessionDescription `json:"sdp,omitempty"`
	Candidate *webrtc.ICECandidateInit   `json:"candidate,omitempty"`
	Cam       *int                       `json:"cam,omitempty"`
	Message   string                     `json:"message,omitempty"`
}

func main() {
	server := flag.String("server", "ws://127.0.0.1:8080/ws", "relay WS URL")
	out := flag.String("out", "/tmp/received.h264", "保存先H.264ファイル")
	dur := flag.Duration("dur", 5*time.Second, "受信時間")
	id := flag.String("id", "KK01", "視聴対象の号機ID (KK0N)")
	cam := flag.Int("cam", -1, "接続後に切替えるカメラ番号(>=0で有効)")
	camAt := flag.Duration("camAt", 2*time.Second, "camChangeを送るまでの待ち時間")
	flag.Parse()

	u, err := url.Parse(*server)
	if err != nil {
		log.Fatal(err)
	}
	conn, _, err := websocket.DefaultDialer.Dial(u.String(), nil)
	if err != nil {
		log.Fatalf("dial: %v", err)
	}
	defer conn.Close()

	m := &webrtc.MediaEngine{}
	if err := m.RegisterDefaultCodecs(); err != nil {
		log.Fatal(err)
	}
	api := webrtc.NewAPI(webrtc.WithMediaEngine(m))
	pc, err := api.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		log.Fatal(err)
	}
	defer pc.Close()

	f, err := os.Create(*out)
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()
	// RTPペイロード→Annex-B(H.264生バイト列)へデパケタイズ。
	// FU-A/STAP-A/単一NALを再構成し、SPS/PPS/IDRを含む正しいストリームを得る。
	depkt := &codecs.H264Packet{}

	var (
		writeMu    sync.Mutex
		pending    []webrtc.ICECandidateInit
		haveRemote bool
		pkts       uint64
		bytes      uint64
	)
	send := func(msg sigMsg) {
		writeMu.Lock()
		defer writeMu.Unlock()
		_ = conn.WriteJSON(msg)
	}

	pc.OnICECandidate(func(c *webrtc.ICECandidate) {
		if c == nil {
			return
		}
		init := c.ToJSON()
		send(sigMsg{Type: "candidate", Candidate: &init})
	})
	pc.OnConnectionStateChange(func(s webrtc.PeerConnectionState) {
		log.Printf("PC state: %s", s)
	})
	pc.OnTrack(func(tr *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		log.Printf("ontrack: %s ssrc=%d", tr.Codec().MimeType, tr.SSRC())
		for {
			pkt, _, readErr := tr.ReadRTP()
			if readErr != nil {
				return
			}
			pkts++
			bytes += uint64(len(pkt.Payload))
			annexb, derr := depkt.Unmarshal(pkt.Payload)
			if derr != nil || len(annexb) == 0 {
				continue
			}
			if _, werr := f.Write(annexb); werr != nil {
				return
			}
		}
	})

	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			var msg sigMsg
			if err := conn.ReadJSON(&msg); err != nil {
				return
			}
			switch msg.Type {
			case "offer":
				_ = pc.SetRemoteDescription(*msg.SDP)
				haveRemote = true
				for _, c := range pending {
					_ = pc.AddICECandidate(c)
				}
				pending = nil
				answer, _ := pc.CreateAnswer(nil)
				_ = pc.SetLocalDescription(answer)
				send(sigMsg{Type: "answer", SDP: pc.LocalDescription()})
			case "candidate":
				if !haveRemote {
					pending = append(pending, *msg.Candidate)
				} else {
					_ = pc.AddICECandidate(*msg.Candidate)
				}
			case "error":
				log.Fatalf("relay error: %s", msg.Message)
			}
		}
	}()

	send(sigMsg{Type: "hello", Role: "viewer", ID: *id})

	// 指定があれば一定時間後にカメラ切替を送る (camChangeテスト用)
	if *cam >= 0 {
		go func() {
			time.Sleep(*camAt)
			c := *cam
			log.Printf("sending camChange -> %d", c)
			send(sigMsg{Type: "camChange", Cam: &c})
		}()
	}

	select {
	case <-time.After(*dur):
	case <-done:
	}

	log.Printf("RESULT: packets=%d payloadBytes=%d -> %s", pkts, bytes, *out)
	if pkts == 0 {
		log.Fatal("FAIL: no RTP received")
	}
}
