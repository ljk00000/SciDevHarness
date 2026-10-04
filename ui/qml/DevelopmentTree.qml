import QtQuick

pragma ComponentBehavior: Bound

Item {
    id: root

    property var nodes: []
    property var layoutOffsets: ({})
    property string selectedId: ""
    property string hoverNodeId: ""
    property string visualTheme: "studio"
    property bool lightMode: visualTheme === "paper"
    property bool focusMode: visualTheme === "focus"
    property real panX: 0
    property real panY: 0
    property real zoom: 1.0
    property real uiScale: 1.0
    readonly property real renderScale: zoom * uiScale
    property real pulse: 0.0
    property int layoutVersion: 0
    property real cardHeight: 68
    property real cardWidth: width < 620
                            ? Math.min(270, Math.max(132, (width - 44) / 2))
                            : Math.min(270, Math.max(180, width * 0.31))
    property real trunkX: width / 2
    property bool compactHeader: width < 520 || height < 360

    signal nodeSelected(string nodeId)
    signal nodeMoved(string nodeId, real offsetX, real offsetY)

    function setLayoutOffsets(offsets) {
        layoutOffsets = offsets || ({})
        graph.requestPaint()
    }

    function offsetFor(nodeId, axis) {
        var stored = layoutOffsets[nodeId]
        if (stored !== undefined) {
            return axis === "x" ? Number(stored.x || 0) : Number(stored.y || 0)
        }
        for (var index = 0; index < nodes.length; index++) {
            if (String(nodes[index].id) === String(nodeId)) {
                return axis === "x" ? Number(nodes[index].offset_x || 0) : Number(nodes[index].offset_y || 0)
            }
        }
        return 0
    }

    function mainNodes() {
        var result = []
        for (var index = 0; index < nodes.length; index++) {
            if (nodes[index].lane === "main") {
                result.push(nodes[index])
            }
        }
        return result
    }

    function mainIndexFor(nodeId) {
        var main = mainNodes()
        for (var index = 0; index < main.length; index++) {
            if (String(main[index].id) === String(nodeId)) {
                return index
            }
        }
        return -1
    }

    function attemptsForParent(parentId) {
        var count = 0
        for (var index = 0; index < nodes.length; index++) {
            if (nodes[index].lane === "attempt" && String(nodes[index].parent_id || "root") === String(parentId)) {
                count++
            }
        }
        return count
    }

    function attemptSlotFor(node) {
        var parentId = String(node.parent_id || "root")
        var slot = 0
        for (var index = 0; index < nodes.length; index++) {
            var candidate = nodes[index]
            if (candidate.lane === "attempt" && String(candidate.parent_id || "root") === parentId) {
                if (String(candidate.id) === String(node.id)) {
                    return slot
                }
                slot++
            }
        }
        return slot
    }

    function mainYFor(nodeId) {
        var main = mainNodes()
        var targetIndex = mainIndexFor(nodeId)
        var y = root.compactHeader ? 48 : 88
        var mainRowHeight = root.compactHeader ? 80 : 122
        var attemptSlotHeight = 90
        for (var index = 0; index < targetIndex; index++) {
            y += mainRowHeight + Math.max(0, attemptsForParent(String(main[index].id)) - 1) * attemptSlotHeight
        }
        return y
    }

    function dotXFor(node) {
        if (node.lane === "main") {
            return trunkX
        }
        return trunkX + 78 + offsetFor(node.id, "x")
    }

    function dotYFor(node) {
        if (node.lane === "main") {
            return mainYFor(node.id)
        }
        var main = mainNodes()
        var parentId = String(node.parent_id || (main.length ? main[main.length - 1].id : "root"))
        var parentY = mainYFor(parentId)
        var attemptOffset = root.compactHeader ? 40 : 54
        var attemptSlotHeight = 90
        return Math.max(90, parentY + attemptOffset + attemptSlotFor(node) * attemptSlotHeight + offsetFor(node.id, "y"))
    }

    function cardXFor(node) {
        var dotX = dotXFor(node)
        if (node.lane === "main") {
            return Math.max(12, dotX - root.cardWidth - 58)
        }
        return Math.min(root.width - root.cardWidth - 12, dotX + 24)
    }

    function cardYFor(node) {
        return dotYFor(node) - cardHeight / 2
    }

    function accentFor(node) {
        if (node.lane === "main") {
            return "#55e3c1"
        }
        if (node.status === "failed") {
            return "#ff8978"
        }
        if (node.status === "retry") {
            return "#f2ca78"
        }
        if (node.status === "active") {
            return "#70b8ff"
        }
        return "#a7b1b8"
    }

    function statusFor(node) {
        if (node.lane === "main") {
            return "当前主线"
        }
        if (node.status === "failed") {
            return "已取消 / 失败"
        }
        if (node.status === "retry") {
            return "等待重试"
        }
        if (node.status === "active") {
            return "进行中"
        }
        return "尝试方向"
    }

    function badgeFor(node) {
        if (node.lane === "main") {
            return "主线"
        }
        if (node.status === "failed") {
            return "失败"
        }
        if (node.status === "retry") {
            return "重试"
        }
        if (node.status === "active") {
            return "进行中"
        }
        return "尝试"
    }

    function setPreviewOffset(nodeId, offsetX, offsetY) {
        var next = ({})
        for (var key in layoutOffsets) {
            next[key] = layoutOffsets[key]
        }
        next[nodeId] = {"x": offsetX, "y": offsetY}
        layoutOffsets = next
        graph.requestPaint()
    }

    function cubicPoint(p0, p1, p2, p3, amount) {
        var inverse = 1 - amount
        return {
            x: inverse * inverse * inverse * p0.x
                + 3 * inverse * inverse * amount * p1.x
                + 3 * inverse * amount * amount * p2.x
                + amount * amount * amount * p3.x,
            y: inverse * inverse * inverse * p0.y
                + 3 * inverse * inverse * amount * p1.y
                + 3 * inverse * amount * amount * p2.y
                + amount * amount * amount * p3.y
        }
    }

    function drawPulse(ctx, point, color, radius) {
        var glow = ctx.createRadialGradient(point.x, point.y, 0, point.x, point.y, radius * 5)
        glow.addColorStop(0, color)
        glow.addColorStop(0.42, Qt.rgba(1, 1, 1, 0.18))
        glow.addColorStop(1, Qt.rgba(1, 1, 1, 0))
        ctx.fillStyle = glow
        ctx.beginPath()
        ctx.arc(point.x, point.y, radius * 5, 0, Math.PI * 2)
        ctx.fill()
        ctx.fillStyle = color
        ctx.beginPath()
        ctx.arc(point.x, point.y, radius, 0, Math.PI * 2)
        ctx.fill()
    }

    function drawBranch(ctx, node, parent, current) {
        var side = current.x > parent.x ? 1 : -1
        var control1 = {"x": parent.x + side * 46, "y": parent.y}
        var control2 = {"x": current.x - side * 36, "y": current.y}
        var accent = accentFor(node)

        ctx.save()
        ctx.translate(5, 7)
        ctx.strokeStyle = Qt.rgba(0, 0, 0, 0.52)
        ctx.lineWidth = 10
        ctx.lineCap = "round"
        ctx.beginPath()
        ctx.moveTo(parent.x, parent.y)
        ctx.bezierCurveTo(control1.x, control1.y, control2.x, control2.y, current.x, current.y)
        ctx.stroke()
        ctx.restore()

        ctx.strokeStyle = Qt.rgba(1, 1, 1, String(node.id) === root.hoverNodeId ? 0.14 : 0.04)
        ctx.lineWidth = 10
        ctx.beginPath()
        ctx.moveTo(parent.x, parent.y)
        ctx.bezierCurveTo(control1.x, control1.y, control2.x, control2.y, current.x, current.y)
        ctx.stroke()
        ctx.strokeStyle = accent
        ctx.globalAlpha = String(node.id) === root.hoverNodeId ? 0.78 : 0.48
        ctx.lineWidth = 2.3
        ctx.beginPath()
        ctx.moveTo(parent.x, parent.y)
        ctx.bezierCurveTo(control1.x, control1.y, control2.x, control2.y, current.x, current.y)
        ctx.stroke()
        ctx.globalAlpha = 1.0

        if (node.status === "active" || node.status === "retry" || String(node.id) === root.hoverNodeId) {
            var seed = 0
            for (var index = 0; index < String(node.id).length; index++) {
                seed += String(node.id).charCodeAt(index)
            }
            var progress = (root.pulse * 0.65 + (seed % 100) / 100.0) % 1.0
            drawPulse(ctx, cubicPoint(parent, control1, control2, current, progress), accent, 2.0)
        }
    }

    function drawDot(ctx, node, point) {
        var accent = accentFor(node)
        var selected = String(node.id) === root.selectedId
        var hovered = String(node.id) === root.hoverNodeId
        if (selected || hovered) {
            var wave = (Math.sin(root.pulse * Math.PI * 2 * 1.35) + 1) / 2
            var radius = selected ? 18 + wave * 7 : 15 + wave * 4
            var halo = ctx.createRadialGradient(point.x, point.y, 0, point.x, point.y, radius)
            halo.addColorStop(0, Qt.rgba(1, 1, 1, selected ? 0.20 : 0.14))
            halo.addColorStop(0.52, accent)
            halo.addColorStop(1, Qt.rgba(1, 1, 1, 0))
            ctx.fillStyle = halo
            ctx.globalAlpha = selected ? 0.52 : 0.34
            ctx.beginPath()
            ctx.arc(point.x, point.y, radius, 0, Math.PI * 2)
            ctx.fill()
            ctx.globalAlpha = 1.0
        }

        ctx.fillStyle = Qt.rgba(0, 0, 0, 0.55)
        ctx.beginPath()
        ctx.arc(point.x + 4, point.y + 6, 12, 0, Math.PI * 2)
        ctx.fill()
        ctx.fillStyle = root.lightMode ? "#f4f7fa" : (root.focusMode ? "#191824" : "#10171a")
        ctx.strokeStyle = accent
        ctx.lineWidth = 3
        ctx.beginPath()
        ctx.arc(point.x, point.y, 9, 0, Math.PI * 2)
        ctx.fill()
        ctx.stroke()
        ctx.fillStyle = accent
        ctx.beginPath()
        ctx.arc(point.x, point.y, 4, 0, Math.PI * 2)
        ctx.fill()
    }

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop {
                position: 0.0
                color: root.lightMode ? "#f9fbfd" : (root.focusMode ? "#191725" : "#1b2732")
            }
            GradientStop {
                position: 0.18
                color: root.lightMode ? "#f5f8fb" : (root.focusMode ? "#151420" : "#17212a")
            }
            GradientStop {
                position: 0.55
                color: root.lightMode ? "#f1f5f9" : (root.focusMode ? "#11121b" : "#121a21")
            }
            GradientStop {
                position: 1.0
                color: root.lightMode ? "#edf2f7" : (root.focusMode ? "#0e1018" : "#0f151b")
            }
        }
    }

    Canvas {
        id: graph
        anchors.fill: parent
        z: 1
        renderTarget: Canvas.FramebufferObject

        onPaint: {
            var ctx = getContext("2d")
            ctx.reset()
            ctx.clearRect(0, 0, width, height)

            var horizon = 54
            var vanishingX = width * 0.52
            for (var gridX = -width; gridX <= width * 2; gridX += 36) {
                var endX = vanishingX + (gridX - vanishingX) * 0.42
                ctx.strokeStyle = root.lightMode
                        ? Qt.rgba(0.27, 0.39, 0.53, 0.09)
                        : Qt.rgba(0.55, 0.70, 0.74, 0.06)
                ctx.lineWidth = 1
                ctx.beginPath()
                ctx.moveTo(gridX, horizon)
                ctx.lineTo(endX, height)
                ctx.stroke()
            }
            for (var gridIndex = 0; gridIndex < 13; gridIndex++) {
                var ratio = Math.pow(gridIndex / 12.0, 1.7)
                var gridY = horizon + (height - horizon) * ratio
                ctx.strokeStyle = root.lightMode
                        ? Qt.rgba(0.27, 0.39, 0.53, 0.035 + gridIndex * 0.004)
                        : Qt.rgba(0.55, 0.70, 0.74, 0.035 + gridIndex * 0.003)
                ctx.beginPath()
                ctx.moveTo(0, gridY)
                ctx.lineTo(width, gridY)
                ctx.stroke()
            }
            var horizonColor = root.lightMode
                    ? Qt.rgba(0.12, 0.51, 0.55, 0.22)
                    : Qt.rgba(0.36, 0.86, 0.75, 0.22)
            ctx.strokeStyle = horizonColor
            ctx.beginPath()
            ctx.moveTo(0, horizon)
            ctx.lineTo(width, horizon)
            ctx.stroke()

            var main = root.mainNodes()
            if (!main.length) {
                ctx.fillStyle = root.lightMode ? "#718096" : "#9da5aa"
                ctx.font = (14 * root.uiScale) + "px Segoe UI"
                ctx.fillText("还没有开发记录", width / 2 - 48, height / 2)
                return
            }

            ctx.save()
            ctx.translate(root.panX, root.panY)
            ctx.scale(root.renderScale, root.renderScale)
            var firstY = root.dotYFor(main[0])
            var lastY = root.dotYFor(main[main.length - 1])
            var trunk = {"x": root.trunkX, "y": firstY}
            var trunkEnd = {"x": root.trunkX, "y": lastY}

            ctx.strokeStyle = Qt.rgba(0, 0, 0, 0.56)
            ctx.lineWidth = 13
            ctx.lineCap = "round"
            ctx.beginPath()
            ctx.moveTo(trunk.x + 5, trunk.y + 8)
            ctx.lineTo(trunkEnd.x + 5, trunkEnd.y + 8)
            ctx.stroke()
            ctx.strokeStyle = root.lightMode ? "#d6e0e8" : (root.focusMode ? "#171523" : "#0b1115")
            ctx.lineWidth = 11
            ctx.beginPath()
            ctx.moveTo(trunk.x, trunk.y)
            ctx.lineTo(trunkEnd.x, trunkEnd.y)
            ctx.stroke()
            ctx.strokeStyle = root.lightMode ? "#73ad9f" : (root.focusMode ? "#3f3b5b" : "#284a45")
            ctx.lineWidth = 7
            ctx.beginPath()
            ctx.moveTo(trunk.x, trunk.y)
            ctx.lineTo(trunkEnd.x, trunkEnd.y)
            ctx.stroke()
            ctx.strokeStyle = root.lightMode ? "#178774" : (root.focusMode ? "#b39aff" : "#5bd9bd")
            ctx.lineWidth = 2.4
            ctx.beginPath()
            ctx.moveTo(trunk.x, trunk.y)
            ctx.lineTo(trunkEnd.x, trunkEnd.y)
            ctx.stroke()

            for (var mainIndex = 0; mainIndex < main.length; mainIndex++) {
                var mainPoint = {"x": root.trunkX, "y": root.dotYFor(main[mainIndex])}
                ctx.strokeStyle = Qt.rgba(0.55, 1.0, 0.88, 0.36)
                ctx.lineWidth = 1
                ctx.beginPath()
                ctx.moveTo(mainPoint.x - 14, mainPoint.y)
                ctx.lineTo(mainPoint.x + 14, mainPoint.y)
                ctx.stroke()
            }

            for (var nodeIndex = 0; nodeIndex < root.nodes.length; nodeIndex++) {
                var node = root.nodes[nodeIndex]
                if (node.lane !== "attempt") {
                    continue
                }
                var parentId = String(node.parent_id || main[main.length - 1].id)
                var parent = {"x": root.trunkX, "y": root.mainYFor(parentId)}
                var current = {"x": root.dotXFor(node), "y": root.dotYFor(node)}
                root.drawBranch(ctx, node, parent, current)
            }

            for (var dotIndex = 0; dotIndex < root.nodes.length; dotIndex++) {
                var dotNode = root.nodes[dotIndex]
                root.drawDot(ctx, dotNode, {"x": root.dotXFor(dotNode), "y": root.dotYFor(dotNode)})
            }

            var trunkProgress = (root.pulse * 0.55 + 0.08) % 1.0
            root.drawPulse(ctx, {
                "x": root.trunkX,
                "y": firstY + (lastY - firstY) * trunkProgress
            }, root.lightMode ? "#178774" : (root.focusMode ? "#b39aff" : "#75f2d2"), 2.8)
            ctx.restore()
        }

        Connections {
            target: root
            function onPulseChanged() { graph.requestPaint() }
            function onNodesChanged() { root.layoutVersion += 1; graph.requestPaint() }
            function onLayoutOffsetsChanged() { root.layoutVersion += 1; graph.requestPaint() }
            function onSelectedIdChanged() { graph.requestPaint() }
            function onHoverNodeIdChanged() { graph.requestPaint() }
            function onVisualThemeChanged() { graph.requestPaint() }
            function onPanXChanged() { graph.requestPaint() }
            function onPanYChanged() { graph.requestPaint() }
            function onZoomChanged() { graph.requestPaint() }
        }
    }

    NumberAnimation on pulse {
        from: 0
        to: 1
        duration: 5800
        loops: Animation.Infinite
        running: root.visible
    }

    MouseArea {
        id: panArea
        anchors.fill: parent
        z: 0
        acceptedButtons: Qt.LeftButton
        property real pressX: 0
        property real pressY: 0
        property real startPanX: 0
        property real startPanY: 0

        onPressed: function(mouse) {
            pressX = mouse.x
            pressY = mouse.y
            startPanX = root.panX
            startPanY = root.panY
        }
        onPositionChanged: function(mouse) {
            if (mouse.buttons & Qt.LeftButton) {
                root.panX = startPanX + mouse.x - pressX
                root.panY = startPanY + mouse.y - pressY
                graph.requestPaint()
            }
        }
        onReleased: {
            root.panX = root.panX
            root.panY = root.panY
        }
    }

    Repeater {
        id: cardRepeater
        model: root.nodes
        delegate: Item {
            id: card
            required property var modelData
            property var node: modelData
            property string nodeId: String(node.id)
            property color accent: root.accentFor(node)
            property bool isMain: node.lane === "main"
            property bool dragging: false
            property real startMouseX: 0
            property real startMouseY: 0
            property real startOffsetX: 0
            property real startOffsetY: 0
            // Keep the canvas width and layout revision as direct binding
            // dependencies. This avoids QML caching an initial x value when
            // the QQuickWidget is resized after the repeater is created.
            property real logicalCardX: root.layoutVersion >= 0
                                       ? (card.isMain
                                          ? Math.max(12, root.trunkX - root.cardWidth - 58)
                                          : Math.min(root.width - root.cardWidth - 12, root.trunkX + 78 + root.offsetFor(card.nodeId, "x") + 24))
                                       : 0
            property real logicalCardY: root.layoutVersion >= 0 ? root.cardYFor(node) : 0
            x: root.panX + logicalCardX * root.renderScale
            y: root.panY + logicalCardY * root.renderScale
            width: root.cardWidth * root.renderScale
            height: root.cardHeight * root.renderScale
            z: dragging || nodeId === root.selectedId ? 4 : 2
            scale: mouseArea.containsMouse ? 1.025 : 1.0
            transformOrigin: Item.Center

            Behavior on scale { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }

            Rectangle {
                x: 7 * root.renderScale
                y: 9 * root.renderScale
                width: parent.width
                height: parent.height
                radius: 9 * root.renderScale
                color: root.lightMode ? "#566579" : "#020507"
                opacity: root.lightMode ? 0.13 : 0.64
            }
            Rectangle {
                x: 5 * root.renderScale
                y: 7 * root.renderScale
                width: parent.width
                height: parent.height
                radius: 9 * root.renderScale
                color: card.accent
                opacity: 0.18
            }
            Rectangle {
                anchors.fill: parent
                radius: 9 * root.renderScale
                border.width: (card.nodeId === root.selectedId || mouseArea.containsMouse) ? 1.5 * root.renderScale : root.renderScale
                border.color: card.nodeId === root.selectedId
                              ? card.accent
                              : (mouseArea.containsMouse
                                 ? (root.lightMode ? "#5aa99a" : (root.focusMode ? "#b6a3ff" : "#8bd8d0"))
                                 : (root.lightMode ? "#cbd5df" : (root.focusMode ? "#514d68" : "#53606a")))
                gradient: Gradient {
                    GradientStop {
                        position: 0.0
                        color: root.lightMode
                               ? (card.nodeId === root.selectedId ? "#e3f2ee" : (mouseArea.containsMouse ? "#f1f7fa" : "#ffffff"))
                               : (root.focusMode
                                  ? (card.nodeId === root.selectedId ? "#302b46" : (mouseArea.containsMouse ? "#2c293e" : "#252334"))
                                  : (card.nodeId === root.selectedId ? "#243d42" : (mouseArea.containsMouse ? "#293844" : "#222e39")))
                    }
                    GradientStop {
                        position: 0.48
                        color: root.lightMode
                               ? (card.nodeId === root.selectedId ? "#eff7f4" : "#f8fafc")
                               : (root.focusMode ? "#211f30" : (card.nodeId === root.selectedId ? "#20343a" : "#1e2933"))
                    }
                    GradientStop {
                        position: 1.0
                        color: root.lightMode
                               ? (card.nodeId === root.selectedId ? "#e8f2ef" : "#f0f4f8")
                               : (root.focusMode ? "#191824" : (card.nodeId === root.selectedId ? "#18262d" : "#151e26"))
                    }
                }
            }
            Rectangle {
                x: 0
                y: 0
                width: 4 * root.renderScale
                height: parent.height
                radius: 2 * root.renderScale
                color: card.accent
            }
            Rectangle {
                x: 10 * root.renderScale
                y: 1 * root.renderScale
                width: Math.max(10 * root.renderScale, parent.width - 20 * root.renderScale)
                height: 1 * root.renderScale
                color: Qt.rgba(1, 1, 1, root.lightMode ? 0.75 : 0.16)
            }
            Rectangle {
                id: badge
                x: parent.width - width - 10 * root.renderScale
                y: 8 * root.renderScale
                width: Math.max(34 * root.renderScale, badgeText.implicitWidth + 14 * root.renderScale)
                height: 18 * root.renderScale
                radius: 9 * root.renderScale
                color: Qt.rgba(card.accent.r, card.accent.g, card.accent.b, card.nodeId === root.selectedId ? 0.24 : 0.14)
                border.width: root.renderScale
                border.color: Qt.rgba(card.accent.r, card.accent.g, card.accent.b, 0.70)

                Text {
                    id: badgeText
                    anchors.centerIn: parent
                    text: root.badgeFor(card.node)
                    color: card.accent
                    font.family: "Segoe UI"
                    font.pixelSize: Math.max(10 * root.uiScale, 10.5 * root.renderScale)
                }
            }
            Text {
                x: 13 * root.renderScale
                y: 7 * root.renderScale
                width: Math.max(40 * root.renderScale, badge.x - 20 * root.renderScale)
                height: 19 * root.renderScale
                text: String(card.node.title || "")
                color: root.lightMode ? "#25364a" : (root.focusMode ? "#f1edff" : "#f0f5fa")
                font.family: "Segoe UI"
                font.bold: true
                font.pixelSize: Math.max(11 * root.uiScale, 12 * root.renderScale)
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }
            Text {
                x: 13 * root.renderScale
                y: 31 * root.renderScale
                width: Math.max(40 * root.renderScale, parent.width - 28 * root.renderScale)
                height: Math.max(10 * root.uiScale, 16 * root.renderScale)
                text: root.statusFor(card.node)
                color: card.accent
                font.family: "Segoe UI"
                font.pixelSize: Math.max(10 * root.uiScale, 10.5 * root.renderScale)
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }
            Text {
                x: 13 * root.renderScale
                y: 49 * root.renderScale
                width: Math.max(40 * root.renderScale, parent.width - 28 * root.renderScale)
                height: Math.max(10 * root.uiScale, 14 * root.renderScale)
                text: String(card.node.meta || "")
                color: root.lightMode ? "#65758a" : "#b0bdc9"
                font.family: "Segoe UI"
                font.pixelSize: Math.max(10 * root.uiScale, 10 * root.renderScale)
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }
            Row {
                visible: !card.isMain
                x: parent.width - 17 * root.renderScale
                y: 23 * root.renderScale
                spacing: 3 * root.renderScale
                Repeater {
                    model: 3
                    delegate: Rectangle {
                        width: 3 * root.renderScale
                        height: 3 * root.renderScale
                        radius: 2 * root.renderScale
                        color: Qt.rgba(0.82, 0.88, 0.90, 0.62)
                    }
                }
            }

            MouseArea {
                id: mouseArea
                anchors.fill: parent
                hoverEnabled: true
                acceptedButtons: Qt.LeftButton
                cursorShape: card.isMain ? Qt.PointingHandCursor : Qt.SizeAllCursor
                onEntered: root.hoverNodeId = card.nodeId
                onExited: if (!card.dragging) root.hoverNodeId = ""
                onPressed: function(mouse) {
                    card.startMouseX = mouse.x
                    card.startMouseY = mouse.y
                    card.startOffsetX = root.offsetFor(card.nodeId, "x")
                    card.startOffsetY = root.offsetFor(card.nodeId, "y")
                    card.dragging = !card.isMain
                }
                onPositionChanged: function(mouse) {
                    if (card.dragging && (mouse.buttons & Qt.LeftButton)) {
                        root.setPreviewOffset(
                            card.nodeId,
                            card.startOffsetX + (mouse.x - card.startMouseX) / root.renderScale,
                            card.startOffsetY + (mouse.y - card.startMouseY) / root.renderScale
                        )
                    }
                }
                onReleased: {
                    if (card.dragging) {
                        root.nodeMoved(card.nodeId, root.offsetFor(card.nodeId, "x"), root.offsetFor(card.nodeId, "y"))
                    }
                    card.dragging = false
                    root.selectedId = card.nodeId
                    root.nodeSelected(card.nodeId)
                }
                onCanceled: card.dragging = false
            }
        }
    }

    Text {
        z: 5
        visible: !root.compactHeader
        x: 18 * root.uiScale
        y: 10 * root.uiScale
        text: "开发尝试树"
        color: root.lightMode ? "#31596a" : (root.focusMode ? "#e7ddff" : "#e2f3ef")
        font.family: "Segoe UI"
        font.bold: true
        font.pixelSize: 15 * root.uiScale
    }
    Text {
        z: 5
        anchors.right: parent.right
        anchors.rightMargin: 18 * root.uiScale
        y: 11 * root.uiScale
        text: root.compactHeader
              ? Math.round(root.zoom * 100) + "% · Ctrl+滚轮"
              : Math.round(root.zoom * 100) + "%  ·  Ctrl+滚轮缩放 · 拖动分支 / 平移画布"
        color: root.lightMode ? "#65758a" : "#a2afbd"
        font.family: "Segoe UI"
        font.pixelSize: (root.compactHeader ? 11 : 13) * root.uiScale
    }
    Text {
        z: 5
        visible: !root.compactHeader
        x: 18 * root.uiScale
        y: 32 * root.uiScale
        width: Math.max(100 * root.uiScale, parent.width - 36 * root.uiScale)
        text: "主干 = 当前编码主线  ·  分支 = 已取消、失败或等待中的尝试方向"
        color: root.lightMode ? "#65758a" : "#a2afbd"
        font.family: "Segoe UI"
        font.pixelSize: 12 * root.uiScale
        elide: Text.ElideRight
    }
}
