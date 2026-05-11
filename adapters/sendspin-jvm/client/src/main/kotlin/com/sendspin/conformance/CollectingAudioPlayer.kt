package com.sendspin.conformance

import com.sendspin.protocol.AudioBuffer
import com.sendspin.protocol.AudioPlayer
import com.sendspin.protocol.StreamFormat

/**
 * Audio player for the conformance CLI. Does not play audio.
 *
 * Actual chunk collection is done via [com.sendspin.protocol.SendSpinClient.onAudioChunk],
 * which bypasses [AudioBuffer] drop-logic. This class exists only to satisfy the
 * [AudioPlayer] interface required by [com.sendspin.protocol.SendSpinClient].
 */
class CollectingAudioPlayer(val audioBuffer: AudioBuffer) : AudioPlayer {
    // Always true so SendSpinClient takes the transition() path on subsequent stream/start
    // messages rather than configure()+start(), which in a real player would flush buffers.
    // Chunks are collected via SendSpinClient.onAudioChunk outside the normal lifecycle.
    override val isPlaying: Boolean = true
    override val droppedDecodeFrames: Long = 0L

    var format: StreamFormat? = null
        private set

    override fun configure(format: StreamFormat) { this.format = format }
    override fun start() {}
    override fun flush() {}
    override fun stop() {}
    override fun transition(format: StreamFormat) { this.format = format }
}
