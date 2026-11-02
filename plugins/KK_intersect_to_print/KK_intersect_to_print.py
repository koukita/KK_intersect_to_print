import json
import os
import tempfile

# QGISで使う地図、レイヤ、座標変換などの機能を読み込みます。
from qgis.PyQt.QtCore import QSize, Qt, QVariant
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction
from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsLayoutExporter,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapToolEmitPoint

CHECKED = Qt.Checked if hasattr(Qt, "Checked") else Qt.CheckState.Checked
UNCHECKED = Qt.Unchecked if hasattr(Qt, "Unchecked") else Qt.CheckState.Unchecked
# Qtのバージョンにより定数の置き場所が異なるため、新旧両方に対応します。
ITEM_IS_USER_CHECKABLE = (
    Qt.ItemIsUserCheckable
    if hasattr(Qt, "ItemIsUserCheckable")
    else Qt.ItemFlag.ItemIsUserCheckable
)
USER_ROLE = Qt.UserRole if hasattr(Qt, "UserRole") else Qt.ItemDataRole.UserRole
SIZE_POLICY_IGNORED = (
    QSizePolicy.Policy.Ignored
    if hasattr(QSizePolicy, "Policy")
    else QSizePolicy.Ignored
)
SIZE_POLICY_PREFERRED = (
    QSizePolicy.Policy.Preferred
    if hasattr(QSizePolicy, "Policy")
    else QSizePolicy.Preferred
)

# QGISのバージョンによる文字列型の違いを吸収し、文字列フィールドを作ります。
try:
    from qgis.PyQt.QtCore import QMetaType

    STRING_FIELD_TYPE = QMetaType.Type.QString
except (ImportError, AttributeError):
    STRING_FIELD_TYPE = QVariant.String


class KKIntersectToPrint:
    """クリック地点で各レイヤを検索し、結果をポイントと帳票に出力します。"""

    def __init__(self, iface):
        # iface はQGIS本体の画面や地図キャンバスを操作するための窓口です。
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.project = QgsProject.instance()
        self.layout_manager = self.project.layoutManager()

        # GUI部品は initGui() で作成するため、作成前は None にしておきます。
        self.dock = None
        self.action = None
        self.toolbar = None
        self.layer_combo = None
        self.search_layers_list = None
        self.refresh_search_layers_button = None
        self.check_visible_layers_button = None
        self.search_layer_checks = {}
        self.saved_point_layer_name = None
        self.saved_point_layer_id = None
        self.saved_point_layer_source = None
        self.saved_point_layer_provider = None
        self.restore_saved_point_layer = False
        self.saved_layout_name = None
        self.keep_point_layer_selection_empty = True
        self.saved_search_layer_checks = {}
        self.click_button = None
        self.load_settings_button = None
        self.save_settings_button = None
        self.print_button = None
        self.layout_combo = None
        self.map_tool = None
        self.previous_map_tool = None

    def initGui(self):
        """ドック、ボタン、選択欄を作り、ボタン操作とQGISのイベントを接続します。"""
        # プラグインの操作パネルを作ります。QVBoxLayoutは部品を上から並べます。
        self.dock = QDockWidget("KK_intersect_to_print", self.iface.mainWindow())
        self.dock.setObjectName("KK_intersect_to_print_dock")
        panel = QWidget(self.dock)
        layout = QVBoxLayout(panel)

        # 結果を書き込むポイントレイヤを選ぶ欄と、新規作成ボタンです。
        layout.addWidget(QLabel("ポイントレイヤ", panel))

        point_layer_controls = QHBoxLayout()
        self.layer_combo = QComboBox(panel)
        point_layer_controls.addWidget(self.layer_combo, 1)
        self.create_point_layer_button = QPushButton("ポイントレイヤ新規作成", panel)
        point_layer_controls.addWidget(self.create_point_layer_button)
        layout.addLayout(point_layer_controls)

        # クリック地点で検索したいレイヤを、チェックボックス付きで表示します。
        search_layers_heading = QHBoxLayout()
        search_layers_heading.addWidget(
            QLabel("属性を取得するポリゴンレイヤ", panel)
        )
        search_layers_heading.addStretch(1)
        self.refresh_search_layers_button = QPushButton("レイヤリスト更新", panel)
        self.refresh_search_layers_button.setToolTip(
            "ポリゴンレイヤの一覧を更新します（チェック状態は維持）"
        )
        search_layers_heading.addWidget(self.refresh_search_layers_button)
        layout.addLayout(search_layers_heading)
        self.search_layers_list = QListWidget(panel)
        layout.addWidget(self.search_layers_list)

        search_layer_controls = QHBoxLayout()
        self.uncheck_all_button = QPushButton("全てのチェックを外す", panel)
        self.check_all_button = QPushButton("全てにチェックする", panel)
        self.check_visible_layers_button = QPushButton("表示レイヤにチェックする", panel)
        for button in (
            self.uncheck_all_button,
            self.check_all_button,
            self.check_visible_layers_button,
        ):
            button.setSizePolicy(SIZE_POLICY_IGNORED, SIZE_POLICY_PREFERRED)
        search_layer_controls.addWidget(self.uncheck_all_button, 1)
        search_layer_controls.addWidget(self.check_all_button, 1)
        layout.addLayout(search_layer_controls)
        visible_button_controls = QHBoxLayout()
        visible_button_controls.addStretch(1)
        visible_button_controls.addWidget(self.check_visible_layers_button, 1)
        layout.addLayout(visible_button_controls)

        # 「設定保存」は右側に寄せ、レイヤの選択状態などを保存します。
        settings_controls = QHBoxLayout()
        settings_controls.addStretch(1)
        self.load_settings_button = QPushButton("設定読み込み", panel)
        self.load_settings_button.setIcon(
            QIcon(os.path.join(os.path.dirname(__file__), "SVG", "folder.svg"))
        )
        settings_controls.addWidget(self.load_settings_button, 1)
        self.save_settings_button = QPushButton("設定保存", panel)
        self.save_settings_button.setIcon(
            QIcon(os.path.join(os.path.dirname(__file__), "SVG", "floppy_disk.svg"))
        )
        settings_controls.addWidget(self.save_settings_button, 1)
        settings_icon_size = QSize(20, 20)
        self.load_settings_button.setIconSize(settings_icon_size)
        self.save_settings_button.setIconSize(settings_icon_size)
        layout.addLayout(settings_controls)

        # 地図クリックとPDF出力のボタンを同じ高さで横に並べます。
        button_icon_size = 24
        self.click_button = QPushButton("ポイント追加", panel)
        self.click_button.setToolTip("地図をクリックしてポイントを追加")
        self.click_button.setIcon(
            QIcon(os.path.join(os.path.dirname(__file__), "SVG", "pin.svg"))
        )
        self.click_button.setIconSize(QSize(button_icon_size, button_icon_size))
        self.print_button = QPushButton("プリント（PDF保存）", panel)
        self.print_button.setIcon(
            QIcon(os.path.join(os.path.dirname(__file__), "SVG", "printer.svg"))
        )
        self.print_button.setIconSize(self.click_button.iconSize())
        button_height = self.click_button.sizeHint().height() * 2
        self.click_button.setFixedHeight(button_height)
        self.print_button.setFixedHeight(button_height)
        action_buttons = QHBoxLayout()
        action_buttons.addWidget(self.click_button, 1)
        action_buttons.addWidget(self.print_button, 1)
        layout.addLayout(action_buttons)
        # 印刷ボタンで使うレイアウトを選ぶ欄です。
        layout.addWidget(QLabel("レイアウト", panel))
        self.layout_combo = QComboBox(panel)
        layout.addWidget(self.layout_combo)
        layout.addWidget(
            QLabel(
                "・はじめに「ポイントレイヤ新規作成」ボタンをクリックして、ポイントレイヤを作成してください。\n"
                "・新規作成したポイントレイヤは一時レイヤなので、必要ならば保存してください。\n"
                "・レイアウトでは、地図帳機能のカバレッジレイヤにポイントレイヤを指定してください。",
                panel,
            )
        )
        layout.addStretch(1)
        self.dock.setWidget(panel)

        # メニューと専用ツールバーの同じ操作からパネルを開閉できるようにします。
        self.action = QAction(
            QIcon(os.path.join(os.path.dirname(__file__), "icon.png")),
            "KK_intersect_to_print",
            self.iface.mainWindow(),
        )
        self.action.setCheckable(True)
        self.action.setChecked(True)
        self.action.toggled.connect(self.dock.setVisible)
        self.dock.visibilityChanged.connect(self.action.setChecked)
        self.iface.addPluginToMenu("KK_intersect_to_print", self.action)
        self.toolbar = self.iface.addToolBar("KK_intersect_to_print")
        self.toolbar.setObjectName("KK_intersect_to_print")
        self.toolbar.addAction(self.action)

        # QGISのバージョンに応じた定数で、ドックを画面右側に配置します。
        right_area = (
            Qt.DockWidgetArea.RightDockWidgetArea
            if hasattr(Qt, "DockWidgetArea")
            else Qt.RightDockWidgetArea
        )
        self.iface.addDockWidget(right_area, self.dock)
        # 各ボタンが押されたときに実行する処理を登録します。
        self.map_tool = _PointCaptureTool(self)
        self.uncheck_all_button.clicked.connect(
            lambda: self.set_all_search_layers_checked(False)
        )
        self.check_all_button.clicked.connect(
            lambda: self.set_all_search_layers_checked(True)
        )
        self.check_visible_layers_button.clicked.connect(
            self.check_visible_search_layers
        )
        self.refresh_search_layers_button.clicked.connect(
            self.refresh_search_layers
        )
        self.click_button.clicked.connect(self.start_point_capture)
        self.create_point_layer_button.clicked.connect(
            self.create_or_select_point_layer
        )
        self.load_settings_button.clicked.connect(self.load_settings_from_button)
        self.save_settings_button.clicked.connect(self.save_settings)
        self.print_button.clicked.connect(self.print_atlas)
        self.project.layersAdded.connect(self.refresh_point_layers)
        self.project.layersRemoved.connect(self.refresh_point_layers)
        # パネル作成後にプロジェクトを読み込むと、レイヤ一覧が入れ替わるため再読込します。
        self.project.readProject.connect(self.restore_project_settings)
        self.layout_manager.layoutAdded.connect(self.refresh_layouts)
        self.layout_manager.layoutRemoved.connect(self.refresh_layouts)
        self.layout_manager.layoutRenamed.connect(self.refresh_layouts)
        self.layer_combo.currentIndexChanged.connect(self.refresh_search_layers)
        self.restore_project_settings()

    def unload(self):
        """プラグイン終了時に、画面部品・ツール・イベント接続を片付けます。"""
        # 地図クリック中にプラグインを終了しても、QGISのツール状態を残さないようにします。
        if self.canvas.mapTool() == self.map_tool:
            self.canvas.unsetMapTool(self.map_tool)
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
        if self.action is not None:
            self.iface.removePluginMenu("KK_intersect_to_print", self.action)
            if self.toolbar is not None:
                self.toolbar.removeAction(self.action)
            self.action.deleteLater()
        if self.toolbar is not None:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
        # 登録したイベントを解除し、終了後に消した部品が呼び出されないようにします。
        self.project.layersAdded.disconnect(self.refresh_point_layers)
        self.project.layersRemoved.disconnect(self.refresh_point_layers)
        self.project.readProject.disconnect(self.restore_project_settings)
        self.layout_manager.layoutAdded.disconnect(self.refresh_layouts)
        self.layout_manager.layoutRemoved.disconnect(self.refresh_layouts)
        self.layout_manager.layoutRenamed.disconnect(self.refresh_layouts)

    def refresh_point_layers(self, *_args):
        """現在のプロジェクトから有効なポイントレイヤだけを選択欄に並べ直します。"""
        if self.layer_combo is None:
            return
        # 一覧を作り直しても、選択中のレイヤが残っていれば選択を維持します。
        selected_id = self.layer_combo.currentData()
        # 線や面ではなく、実際にポイント形状を持つベクターレイヤだけを対象にします。
        point_layers = [
            layer
            for layer in self.project.mapLayers().values()
            if layer.isValid()
            and layer.type() == Qgis.LayerType.Vector
            and QgsWkbTypes.geometryType(layer.wkbType()) == QgsWkbTypes.PointGeometry
        ]

        # 項目の入れ替え中に選択変更イベントが連続発生しないよう一時停止します。
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        for layer in point_layers:
            self.layer_combo.addItem(layer.name(), layer.id())
        # 保存したレイヤを探している間は、先頭の別レイヤを誤って選択しないようにします。
        if self.restore_saved_point_layer:
            selected_index = self._find_saved_point_layer_index(point_layers)
            if selected_index >= 0:
                self.restore_saved_point_layer = False
        else:
            selected_index = self.layer_combo.findData(selected_id)
            if selected_index < 0 and self.saved_point_layer_id:
                selected_index = self.layer_combo.findData(self.saved_point_layer_id)
            if selected_index < 0 and self.saved_point_layer_name:
                for index in range(self.layer_combo.count()):
                    if self.layer_combo.itemText(index) == self.saved_point_layer_name:
                        selected_index = index
                        break
            if (
                selected_index < 0
                and self.layer_combo.count()
                and not self.keep_point_layer_selection_empty
            ):
                selected_index = 0
        if selected_index >= 0:
            self.layer_combo.setCurrentIndex(selected_index)
        elif self.keep_point_layer_selection_empty or self.restore_saved_point_layer:
            self.layer_combo.setCurrentIndex(-1)
        # 一覧更新中に停止していたイベント通知を再開します。
        self.layer_combo.blockSignals(False)
        self.refresh_search_layers()

    def _find_saved_point_layer_index(self, point_layers):
        """保存済みのレイヤ情報と照合し、選択すべき項目の位置を返します。"""
        # レイヤIDは最も確実な識別子なので、最初に一致を探します。
        if self.saved_point_layer_id:
            for index, layer in enumerate(point_layers):
                if layer.id() == self.saved_point_layer_id:
                    return index
        # プロジェクトを開き直すとIDが変わることがあるため、データソースも確認します。
        if self.saved_point_layer_source and self.saved_point_layer_provider:
            matching_layers = [
                (index, layer)
                for index, layer in enumerate(point_layers)
                if layer.source() == self.saved_point_layer_source
                and layer.providerType() == self.saved_point_layer_provider
            ]
            named_matches = [
                index
                for index, layer in matching_layers
                if layer.name() == self.saved_point_layer_name
            ]
            if len(named_matches) == 1:
                return named_matches[0]
            # 同じ名前・データソースの候補が複数ある場合は誤選択せず未選択にします。
            if len(named_matches) > 1:
                return -1
            if len(matching_layers) == 1:
                return matching_layers[0][0]
            if matching_layers:
                return -1
        # 最後に名前だけで照合します。ただし名前が一意のときだけ採用します。
        if self.saved_point_layer_name:
            matching_layers = [
                index
                for index, layer in enumerate(point_layers)
                if layer.name() == self.saved_point_layer_name
            ]
            if len(matching_layers) == 1:
                return matching_layers[0]
        return -1

    def restore_project_settings(self, *_args):
        """プロジェクト切り替え時に古い設定を消し、新しいプロジェクトの設定を読みます。"""
        # 前のプロジェクトの情報が新しいプロジェクトに混ざらないよう初期化します。
        self.saved_point_layer_name = None
        self.saved_point_layer_id = None
        self.saved_point_layer_source = None
        self.saved_point_layer_provider = None
        self.restore_saved_point_layer = False
        self.saved_layout_name = None
        self.saved_search_layer_checks = {}
        self.search_layer_checks = {}
        self.keep_point_layer_selection_empty = True
        # 古い一覧の状態を退避せず、JSONの保存値から新しい一覧を作り直します。
        if self.search_layers_list is not None:
            self.search_layers_list.clear()
        # 前のプロジェクトやレイヤ追加時の自動選択を引き継がないようにします。
        if self.layer_combo is not None:
            self.layer_combo.blockSignals(True)
            self.layer_combo.setCurrentIndex(-1)
            self.layer_combo.blockSignals(False)

        # 設定ファイルがある場合だけ読み込みます。無い場合は選択欄を空のままにします。
        settings_path = self._settings_file_path()
        if settings_path and os.path.isfile(settings_path):
            self.load_settings()
        self.refresh_point_layers()
        self.refresh_layouts()

    def _create_default_point_layer(self):
        """「交差ポイント」という一時レイヤを作成し、保存済みスタイルを適用します。"""
        try:
            # memoryプロバイダーのレイヤはQGIS上の一時レイヤです。
            point_layer = QgsVectorLayer(
                "Point",
                "交差ポイント",
                "memory",
            )
            if not point_layer.isValid():
                raise RuntimeError("一時レイヤが無効です。")

            # 新しいレイヤの座標系は、現在のプロジェクトの座標系に合わせます。
            point_layer.setCrs(self.project.crs())
            style_path = os.path.join(os.path.dirname(__file__), "point_style.qml")
            if not os.path.isfile(style_path):
                raise FileNotFoundError(
                    "スタイルファイルが見つかりません: {}".format(style_path)
                )
            style_result = point_layer.loadNamedStyle(style_path)
            if isinstance(style_result, tuple):
                style_message, style_loaded = style_result
                if not style_loaded:
                    raise RuntimeError(
                        "スタイルファイルを読み込めませんでした: {}".format(
                            style_message
                        )
                    )
            elif style_result:
                raise RuntimeError(
                    "スタイルファイルを読み込めませんでした: {}".format(
                        style_result
                    )
                )
            self.project.addMapLayer(point_layer)
            self.saved_point_layer_id = point_layer.id()
            self.saved_point_layer_name = point_layer.name()
            return True
        except Exception as error:
            self._show_error("一時ポイントレイヤを作成できませんでした。", error)
            return False

    def create_or_select_point_layer(self):
        """同名のポイントレイヤがあれば再利用し、無ければ新規作成して選択します。"""
        # 同じ名前のレイヤが既にある場合、重複作成せずにそれを使います。
        existing_point_layer = next(
            (
                layer
                for layer in self.project.mapLayersByName("交差ポイント")
                if layer.isValid()
                and layer.type() == Qgis.LayerType.Vector
                and QgsWkbTypes.geometryType(layer.wkbType())
                == QgsWkbTypes.PointGeometry
            ),
            None,
        )
        if existing_point_layer is None:
            if not self._create_default_point_layer():
                return
        else:
            self.saved_point_layer_id = existing_point_layer.id()
            self.saved_point_layer_name = existing_point_layer.name()
        # この操作後は空欄を維持せず、作成または再利用したレイヤを選択します。
        self.keep_point_layer_selection_empty = False
        self.refresh_point_layers()

    def refresh_search_layers(self, *_args):
        """検索対象にできるポリゴンレイヤをリストに反映します。"""
        if self.search_layers_list is None:
            return
        # リストを作り直す前に、現在のチェック状態をレイヤIDごとに退避します。
        for index in range(self.search_layers_list.count()):
            item = self.search_layers_list.item(index)
            self.search_layer_checks[item.data(USER_ROLE)] = (
                item.checkState() == CHECKED
            )
        self.search_layers_list.clear()
        # 出力先ポイントレイヤ自身は検索対象から除外します。
        point_layer_id = self.layer_combo.currentData()
        for layer in self._layers_in_layer_tree_order():
            if not layer.isValid() or layer.id() == point_layer_id:
                continue
            if (
                layer.type() != Qgis.LayerType.Vector
                or layer.geometryType() != QgsWkbTypes.PolygonGeometry
            ):
                continue

            item = QListWidgetItem(layer.name())
            item.setData(USER_ROLE, layer.id())
            item.setFlags(item.flags() | ITEM_IS_USER_CHECKABLE)
            # まず現在の画面、次に保存設定からチェック状態を探します。
            checked = self.search_layer_checks.get(layer.id())
            if checked is None:
                checked = self.saved_search_layer_checks.get(layer.id())
            if checked is None:
                checked = self.saved_search_layer_checks.get(layer.name(), False)
            item.setCheckState(
                CHECKED if checked else UNCHECKED
            )
            self.search_layers_list.addItem(item)

    def _layers_in_layer_tree_order(self):
        """レイヤパネルの上から下への順序で、レイヤツリー内のレイヤを返します。"""
        layers = []

        def collect_layers(node):
            layer_method = getattr(node, "layer", None)
            if callable(layer_method):
                layer = layer_method()
                if layer is not None:
                    layers.append(layer)
            children_method = getattr(node, "children", None)
            if callable(children_method):
                for child in children_method():
                    collect_layers(child)

        for child in self.project.layerTreeRoot().children():
            collect_layers(child)
        return layers

    def refresh_layouts(self, *_args):
        """プロジェクト内の印刷レイアウトを一覧にして、可能なら選択状態を保ちます。"""
        if self.layout_combo is None:
            return
        # レイアウトが追加・削除・改名された場合も最新一覧を作れるようにします。
        selected_name = self.layout_combo.currentData() or self.saved_layout_name
        self.layout_combo.blockSignals(True)
        self.layout_combo.clear()
        for layout in self.layout_manager.printLayouts():
            self.layout_combo.addItem(layout.name(), layout.name())
        # 保存した名前を優先し、無ければ「帳票」、さらに無ければ先頭を選びます。
        selected_index = self.layout_combo.findData(selected_name)
        if selected_index < 0 and self.layout_combo.count():
            selected_index = self.layout_combo.findData("帳票")
        if selected_index < 0 and self.layout_combo.count():
            selected_index = 0
        if selected_index >= 0:
            self.layout_combo.setCurrentIndex(selected_index)
        self.layout_combo.blockSignals(False)

    def _settings_file_path(self):
        """プロジェクト名を付けた設定ファイルのパスを返します。"""
        project_file = self.project.fileName()
        # プロジェクトがまだ一度も保存されていない場合、保存先を決められません。
        if not project_file:
            return None
        project_name = os.path.splitext(os.path.basename(project_file))[0]
        return os.path.join(
            os.path.dirname(os.path.abspath(project_file)),
            "point_setting_{}.json".format(project_name),
        )

    def load_settings(self):
        """JSON設定を読み込み、型や内容を確認してから復元用の変数に反映します。"""
        settings_path = self._settings_file_path()
        if settings_path is None or not os.path.isfile(settings_path):
            return False
        try:
            # UTF-8で読み込みます。日本語のレイヤ名もそのまま扱えます。
            with open(settings_path, "r", encoding="utf-8") as settings_file:
                settings = json.load(settings_file)
            # JSONの最上位は、各設定を持つ辞書である必要があります。
            if not isinstance(settings, dict):
                raise ValueError("設定ファイルの形式が正しくありません。")

            # ファイルから値を取り出し、後続で形式を検証します。
            point_layer_name = settings.get("point_layer_name")
            point_layer_id = settings.get("point_layer_id")
            point_layer_source = settings.get("point_layer_source")
            point_layer_provider = settings.get("point_layer_provider")
            layout_name = settings.get("layout_name")
            search_layers = settings.get("search_layers", [])
            if (
                point_layer_name is not None
                and not isinstance(point_layer_name, str)
            ):
                raise ValueError("ポイントレイヤ名の形式が正しくありません。")
            if point_layer_id is not None and not isinstance(point_layer_id, str):
                raise ValueError("ポイントレイヤIDの形式が正しくありません。")
            if point_layer_source is not None and not isinstance(
                point_layer_source, str
            ):
                raise ValueError("ポイントレイヤのデータソース形式が正しくありません。")
            if point_layer_provider is not None and not isinstance(
                point_layer_provider, str
            ):
                raise ValueError("ポイントレイヤのプロバイダー形式が正しくありません。")
            if layout_name is not None and not isinstance(layout_name, str):
                raise ValueError("レイアウト名の形式が正しくありません。")
            if not isinstance(search_layers, list):
                raise ValueError("検索レイヤ一覧の形式が正しくありません。")

            # チェック状態はレイヤIDとレイヤ名の両方で記録して復元しやすくします。
            restored_checks = {}
            for layer_setting in search_layers:
                if not isinstance(layer_setting, dict):
                    raise ValueError("検索レイヤ設定の形式が正しくありません。")
                name = layer_setting.get("name")
                layer_id = layer_setting.get("id")
                checked = layer_setting.get("checked")
                if (
                    not isinstance(name, str)
                    or not isinstance(checked, bool)
                    or (layer_id is not None and not isinstance(layer_id, str))
                ):
                    raise ValueError("検索レイヤ設定の値が正しくありません。")
                if layer_id:
                    restored_checks[layer_id] = checked
                restored_checks[name] = checked

            # すべての値が正しい形式なら、画面の再構築で使う保存値に反映します。
            self.saved_point_layer_name = point_layer_name
            self.saved_point_layer_id = point_layer_id
            self.saved_point_layer_source = point_layer_source
            self.saved_point_layer_provider = point_layer_provider
            self.restore_saved_point_layer = bool(
                point_layer_id
                or point_layer_name
                or point_layer_source
                or point_layer_provider
            )
            self.saved_layout_name = layout_name
            self.saved_search_layer_checks = restored_checks
            self.keep_point_layer_selection_empty = (
                point_layer_id is None and point_layer_name is None
            )
            return True
        except (OSError, UnicodeError, ValueError) as error:
            self._show_error("設定ファイルを読み込めませんでした。", error)
            return False

    def load_settings_from_button(self):
        """設定ファイルを読み込み、保存された選択内容を画面へ反映します。"""
        settings_path = self._settings_file_path()
        if settings_path is None:
            self._show_error(
                "プロジェクトを保存してから設定を読み込んでください。"
            )
            return
        if not os.path.isfile(settings_path):
            self._show_error(
                "設定ファイルが見つかりません: {}".format(settings_path)
            )
            return

        # 手動読込時は現在の画面のチェック状態よりJSONの値を優先します。
        self.search_layer_checks = {}
        if not self.load_settings():
            return
        self.refresh_point_layers()
        for index in range(self.search_layers_list.count()):
            item = self.search_layers_list.item(index)
            checked = self.saved_search_layer_checks.get(
                item.data(USER_ROLE),
                self.saved_search_layer_checks.get(item.text(), False),
            )
            item.setCheckState(CHECKED if checked else UNCHECKED)
        self.refresh_layouts()
        self.iface.messageBar().pushSuccess(
            "KK_intersect_to_print", "設定を読み込みました: {}".format(settings_path)
        )

    def save_settings(self):
        """現在選択中のレイヤ・チェック状態・レイアウトをJSONファイルに保存します。"""
        settings_path = self._settings_file_path()
        # プロジェクト未保存では「同じフォルダー」を決められないため、利用者に知らせます。
        if settings_path is None:
            self._show_error(
                "プロジェクトを保存してから設定を保存してください。"
            )
            return

        # 画面に表示中の選択内容を、JSONに書き込める基本データにまとめます。
        point_layer = self._selected_point_layer()
        layout_name = (
            self.layout_combo.currentData() if self.layout_combo is not None else None
        )
        search_layers = []
        if self.search_layers_list is not None:
            for index in range(self.search_layers_list.count()):
                item = self.search_layers_list.item(index)
                search_layers.append(
                    {
                        "id": item.data(USER_ROLE),
                        "name": item.text(),
                        "checked": item.checkState() == CHECKED,
                    }
                )
        settings = {
            "point_layer_id": point_layer.id() if point_layer else None,
            "point_layer_name": point_layer.name() if point_layer else None,
            "point_layer_source": point_layer.source() if point_layer else None,
            "point_layer_provider": (
                point_layer.providerType() if point_layer else None
            ),
            "layout_name": layout_name,
            "search_layers": search_layers,
        }
        # 一時ファイルへ完全に書いてから置き換え、途中失敗で設定を壊さないようにします。
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=os.path.dirname(settings_path),
                prefix=".point_setting-",
                suffix=".tmp",
                delete=False,
            ) as settings_file:
                temporary_path = settings_file.name
                # ensure_ascii=Falseにすると日本語を読める形で保存できます。
                json.dump(settings, settings_file, ensure_ascii=False, indent=2)
                # データをディスクへ書き出してから、設定ファイルとして置き換えます。
                settings_file.flush()
                os.fsync(settings_file.fileno())
            os.replace(temporary_path, settings_path)
            temporary_path = None
            self.iface.messageBar().pushSuccess(
                "KK_intersect_to_print", "設定を保存しました: {}".format(settings_path)
            )
        except (OSError, TypeError, ValueError) as error:
            self._show_error("設定を保存できませんでした。", error)
        finally:
            # 保存に失敗した場合だけ、作成途中の一時ファイルを片付けます。
            if temporary_path is not None:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    self._show_error("一時設定ファイルを削除できませんでした。", error)

    def set_all_search_layers_checked(self, checked):
        """検索対象リストの全項目を、指定されたチェック状態にそろえます。"""
        if self.search_layers_list is None:
            return
        check_state = CHECKED if checked else UNCHECKED
        for index in range(self.search_layers_list.count()):
            item = self.search_layers_list.item(index)
            item.setCheckState(check_state)
            self.search_layer_checks[item.data(USER_ROLE)] = checked

    def check_visible_search_layers(self):
        """レイヤパネルで表示状態のレイヤだけを検索対象にチェックします。"""
        if self.search_layers_list is None:
            return
        layer_tree_root = self.project.layerTreeRoot()
        for index in range(self.search_layers_list.count()):
            item = self.search_layers_list.item(index)
            layer_node = layer_tree_root.findLayer(item.data(USER_ROLE))
            checked = layer_node is not None and layer_node.isVisible()
            item.setCheckState(CHECKED if checked else UNCHECKED)
            self.search_layer_checks[item.data(USER_ROLE)] = checked

    def start_point_capture(self):
        """地図クリックをポイント追加として受け取るツールを有効にします。"""
        layer = self._selected_point_layer()
        if layer is None:
            self._show_error(
                "ポイントレイヤを選択するか、「ポイントレイヤ新規作成」を"
                "クリックしてください。"
            )
            return
        try:
            # 後で元の地図ツールへ戻せるよう、現在のツールを記憶します。
            self.previous_map_tool = self.canvas.mapTool()
            self.canvas.setMapTool(self.map_tool)
        except Exception as error:
            self.previous_map_tool = None
            self._show_error("地図クリックツールを開始できませんでした。", error)

    def capture_point(self, canvas_point):
        """クリック位置で検索を行い、結果をポイントレイヤに書き込みます。"""
        layer = self._selected_point_layer()
        if layer is None:
            self._show_error("ポイントレイヤが見つかりません。")
            return

        try:
            if layer.featureCount() >= 2:
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "KK_intersect_to_print",
                    "交差ポイントにデータが多数あります。ポイント作成時にすべてのデータは削除されます。ポイントを作成する場合は、あらかじめデータを全て削除しておいてください。",
                )
                return

            # クリック位置をQGISの図形にし、検索結果を取得してからポイントを更新します。
            point_in_canvas_crs = QgsGeometry.fromPointXY(canvas_point)
            attributes, field_names = self._collect_attributes(
                point_in_canvas_crs, layer
            )
            feature = self._replace_point(
                layer, point_in_canvas_crs, attributes, field_names
            )
            # 作成した地物の属性フォームを開き、結果をすぐ確認できるようにします。
            self.iface.openFeatureForm(layer, feature)
            self.iface.messageBar().pushSuccess(
                "KK_intersect_to_print", "クリック位置のポイントと属性を更新しました。"
            )
        except Exception as error:
            self._show_error("ポイントの作成に失敗しました。", error)
        finally:
            # 成功・失敗にかかわらず、クリック後は元の地図操作ツールに戻します。
            if self.canvas.mapTool() == self.map_tool:
                try:
                    self.canvas.unsetMapTool(self.map_tool)
                    if self.previous_map_tool is not None:
                        self.canvas.setMapTool(self.previous_map_tool)
                except Exception as error:
                    self._show_error("地図ツールを元に戻せませんでした。", error)
                finally:
                    self.previous_map_tool = None

    def _collect_attributes(self, point_in_canvas_crs, target_layer):
        """チェックされたレイヤを検索し、ポイントへ書き込む値と項目名を作ります。"""
        field_names = []
        values_by_field = []
        # 出力先の主キー項目名は検索結果の項目名と重複しないよう予約します。
        used_names = {
            field.name().casefold()
            for index, field in enumerate(target_layer.fields())
            if index in set(target_layer.dataProvider().pkAttributeIndexes())
        }

        # チェックされたレイヤだけを検索します。失敗時には対象レイヤ名も通知します。
        selected_layer_ids = self._selected_search_layer_ids()
        for layer in self.project.mapLayers().values():
            if layer.id() == target_layer.id() or layer.id() not in selected_layer_ids:
                continue

            try:
                if not layer.isValid():
                    raise RuntimeError("レイヤが無効です。")

                if layer.type() == Qgis.LayerType.Vector:
                    if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                        continue
                    # 地図の座標系から検索レイヤの座標系へ変換して検索します。
                    point = self._transform_geometry(
                        point_in_canvas_crs,
                        self.canvas.mapSettings().destinationCrs(),
                        layer.crs(),
                    )
                    # まずクリック点を含む小さな範囲で候補を絞り、検索を軽くします。
                    request = QgsFeatureRequest().setFilterRect(point.boundingBox())
                    source_fields = list(layer.fields())
                    field_values = [[] for _field in source_fields]
                    # 範囲で絞った候補に対して正確な交差判定を行い、属性だけを残します。
                    for feature in layer.getFeatures(request):
                        geometry = feature.geometry()
                        if (
                            geometry
                            and not geometry.isNull()
                            and geometry.intersects(point)
                        ):
                            for index, source_field in enumerate(source_fields):
                                field_values[index].append(
                                    feature.attribute(source_field.name())
                                )

                    # 交差地物が無ければ値はNoneのまま、全項目を出力します。
                    for source_field, values in zip(source_fields, field_values):
                        name = self._unique_field_name(
                            "{}:{}".format(layer.name(), source_field.name()),
                            used_names,
                        )
                        field_names.append(name)
                        values_by_field.append(self._combined_value(values))

            except Exception as error:
                raise RuntimeError(
                    "検索レイヤ「{}」の処理に失敗しました。{}".format(
                        layer.name(), error
                    )
                ) from error

        return values_by_field, field_names

    def _selected_search_layer_ids(self):
        """画面上でチェックされている検索対象レイヤのIDを集合で返します。"""
        if self.search_layers_list is None:
            return set()
        # IDの集合にすることで、検索中に対象かどうかを素早く判定できます。
        return {
            self.search_layers_list.item(index).data(USER_ROLE)
            for index in range(self.search_layers_list.count())
            if self.search_layers_list.item(index).checkState()
            == CHECKED
        }

    def _replace_point(self, layer, point_in_canvas_crs, values, field_names):
        """古い検索結果を置き換え、新しいポイントと属性をレイヤに保存します。"""
        # もともと編集モードだった場合は、ここで勝手に確定しないよう状態を記録します。
        started_editing = not layer.isEditable()
        if started_editing and not layer.startEditing():
            raise RuntimeError("ポイントレイヤを編集モードにできません。")

        # 編集操作を一つのまとまりとして扱い、途中失敗ならまとめて取り消します。
        layer.beginEditCommand("クリック位置と交差属性を更新")
        edit_command_active = True
        try:
            # レイヤには最新のクリック結果だけを残す仕様のため、既存ポイントを削除します。
            existing_ids = [feature.id() for feature in layer.getFeatures()]
            for feature_id in existing_ids:
                if not layer.deleteFeature(feature_id):
                    raise RuntimeError("既存のポイントを削除できませんでした。")

            # 主キーと今回も使用する項目を残し、不要な項目だけ削除します。
            primary_key_indexes = set(layer.dataProvider().pkAttributeIndexes())
            primary_key_names = {
                field.name()
                for index, field in enumerate(layer.fields())
                if index in primary_key_indexes
            }
            preserved_field_names = [
                field.name()
                for field in layer.fields()
                if field.name() in primary_key_names
            ]
            old_field_indexes = [
                index
                for index, field in enumerate(layer.fields())
                if index not in primary_key_indexes and field.name() not in field_names
            ]
            for field_index in reversed(old_field_indexes):
                if not layer.deleteAttribute(field_index):
                    raise RuntimeError(
                        "ポイントレイヤの既存フィールドを削除できませんでした。"
                    )
            existing_field_names = {field.name() for field in layer.fields()}
            # 新しく必要な項目だけを文字列型で追加します。
            for name in field_names:
                if name in existing_field_names:
                    continue
                field = QgsField(name, STRING_FIELD_TYPE, "text")
                if not layer.addAttribute(field):
                    raise RuntimeError(
                        "フィールド「{}」を追加できません。".format(name)
                    )
                existing_field_names.add(name)
            layer.updateFields()
            actual_names = [field.name() for field in layer.fields()]
            expected_names = set(preserved_field_names + field_names)
            if (
                len(actual_names) != len(expected_names)
                or set(actual_names) != expected_names
            ):
                raise RuntimeError(
                    "ポイントレイヤのデータ形式が「レイヤ名:フィールド名」の"
                    "フィールド名に対応していません。"
                )

            # クリック位置を出力先レイヤの座標系に変換します。
            point = self._transform_geometry(
                point_in_canvas_crs,
                self.canvas.mapSettings().destinationCrs(),
                layer.crs(),
            )
            # マルチポイントレイヤには、マルチ形式の図形を追加します。
            if QgsWkbTypes.isMultiType(layer.wkbType()):
                point.convertToMultiType()
            feature = QgsFeature(layer.fields())
            feature.setGeometry(point)
            values_by_name = dict(zip(field_names, values))
            feature.setAttributes(
                [
                    None
                    if field.name() in primary_key_names
                    else values_by_name[field.name()]
                    for field in layer.fields()
                ]
            )
            if not layer.addFeature(feature):
                raise RuntimeError("新しいポイントを追加できませんでした。")

            # 編集コマンドを完了させ、必要なら今回開始した編集を保存します。
            layer.endEditCommand()
            edit_command_active = False
            if started_editing and not layer.commitChanges():
                errors = "\n".join(layer.commitErrors())
                layer.rollBack()
                raise RuntimeError(
                    "ポイントレイヤへの保存に失敗しました。{}".format(
                        "\n" + errors if errors else ""
                    )
                )
            # 書き込み後に地物を読み直し、属性フォームへ渡す地物を取得します。
            created_features = iter(layer.getFeatures())
            created_feature = next(created_features, None)
            if created_feature is None or next(created_features, None) is not None:
                raise RuntimeError("作成したポイントを取得できませんでした。")
            return created_feature
        except Exception as error:
            # 処理途中の失敗で一部だけ更新された状態を残さないようにします。
            cleanup_errors = []
            if edit_command_active:
                try:
                    layer.destroyEditCommand()
                except Exception as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
            if started_editing and layer.isEditable():
                try:
                    layer.rollBack()
                except Exception as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
            if cleanup_errors:
                raise RuntimeError(
                    "編集失敗後のロールバックにも失敗しました: {}".format(
                        "; ".join(cleanup_errors)
                    )
                ) from error
            raise

    def print_atlas(self):
        """選択されたレイアウトをPDFに出力します（地図帳なら先頭地物を出力）。"""
        layout_name = (
            self.layout_combo.currentData() if self.layout_combo is not None else None
        )
        if not layout_name:
            self._show_error("印刷するレイアウトを選択してください。")
            return
        layout = self.layout_manager.layoutByName(layout_name)
        if layout is None:
            self._show_error("レイアウト「{}」が見つかりません。".format(layout_name))
            return
        try:
            # レイアウト編集画面を開き、選択したレイアウトを確認できるようにします。
            self.iface.openLayoutDesigner(layout)
            atlas = layout.atlas() if hasattr(layout, "atlas") else None
            # 地図帳が無効ならレイアウト全体を、そうでなければ地図帳の先頭を出力します。
            if atlas is None or not atlas.enabled():
                default_path = os.path.join(
                    self.project.homePath(), "{}.pdf".format(layout_name)
                )
                output_path, _selected_filter = QFileDialog.getSaveFileName(
                    self.iface.mainWindow(),
                    "レイアウトをPDFで保存",
                    default_path,
                    "PDF (*.pdf)",
                )
                if not output_path:
                    return
                if not output_path.lower().endswith(".pdf"):
                    output_path += ".pdf"
                # QGISのレイアウト出力機能にPDF保存を依頼します。
                settings = QgsLayoutExporter.PdfExportSettings()
                exporter = QgsLayoutExporter(layout)
                export_result = exporter.exportToPdf(output_path, settings)
            else:
                export_result = self._export_first_atlas_feature(layout, atlas)
                if export_result is None:
                    return
                export_result, output_path = export_result

            # QGISのバージョンや出力方法による戻り値の違いを読み分けます。
            if isinstance(export_result, tuple):
                result, export_error = export_result
            else:
                result, export_error = export_result, ""
            if result != QgsLayoutExporter.Success:
                raise RuntimeError(
                    "PDFの書き出しに失敗しました（エラーコード: {}）。{}".format(
                        result, "\n" + export_error if export_error else ""
                    )
                )
            if not output_path:
                return
            self.iface.messageBar().pushSuccess(
                "KK_intersect_to_print", "PDFを保存しました: {}".format(output_path)
            )
        except Exception as error:
            self._show_error("レイアウトをPDFに出力できませんでした。", error)

    def _export_first_atlas_feature(self, layout, atlas):
        """地図帳の先頭地物を特定し、その地物だけをPDFに書き出します。"""
        first_features = []
        # 地図帳を順に処理したときに通知される地物を、一時的に受け取ります。
        capture_first_feature = first_features.append
        atlas.featureChanged.connect(capture_first_feature)
        try:
            # 地図帳の描画を開始し、最初の地物IDを調べます。
            if not atlas.beginRender():
                raise RuntimeError("地図帳の地物を読み込めませんでした。")
            try:
                if not atlas.first() or not first_features:
                    raise RuntimeError("地図帳に出力できる地物がありません。")
                first_feature_id = first_features[-1].id()
            finally:
                atlas.endRender()
        finally:
            atlas.featureChanged.disconnect(capture_first_feature)

        default_path = os.path.join(
            self.project.homePath(), "{}.pdf".format(layout.name())
        )
        output_path, _selected_filter = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "レイアウトをPDFで保存",
            default_path,
            "PDF (*.pdf)",
        )
        if not output_path:
            return None
        if not output_path.lower().endswith(".pdf"):
            output_path += ".pdf"

        # 書き出し後に元へ戻せるよう、既存の抽出条件を保存します。
        original_filter_enabled = atlas.filterFeatures()
        original_filter_expression = atlas.filterExpression()
        try:
            # 今回出力したい最初の地物だけを含む条件を一時的に設定します。
            atlas.setFilterFeatures(True)
            self._set_atlas_filter_expression(
                atlas, "$id = {}".format(first_feature_id)
            )
            settings = QgsLayoutExporter.PdfExportSettings()
            exporter = QgsLayoutExporter(layout)
            export_result = exporter.exportToPdf(atlas, output_path, settings)
        finally:
            # PDF出力に失敗しても、レイアウトの抽出設定は必ず元に戻します。
            try:
                self._set_atlas_filter_expression(atlas, original_filter_expression)
            finally:
                atlas.setFilterFeatures(original_filter_enabled)
        return (export_result, output_path)

    def _selected_point_layer(self):
        """コンボボックスで選択中のレイヤを取得し、使えるポイントか確認します。"""
        if self.layer_combo is None:
            return None
        layer_id = self.layer_combo.currentData()
        layer = self.project.mapLayer(layer_id) if layer_id else None
        # 選択解除・削除・無効化など、検索に使えない状態ならNoneを返します。
        if (
            layer is None
            or not layer.isValid()
            or layer.type() != Qgis.LayerType.Vector
            or QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PointGeometry
        ):
            return None
        return layer

    @staticmethod
    def _transform_geometry(geometry, source_crs, destination_crs):
        """元の図形を壊さずコピーし、必要な場合だけ座標系を変換します。"""
        transformed = QgsGeometry(geometry)
        if source_crs != destination_crs:
            transformed.transform(
                QgsCoordinateTransform(
                    source_crs, destination_crs, QgsProject.instance()
                )
            )
        return transformed

    @staticmethod
    def _combined_value(values):
        """複数の交差地物の属性値を、セミコロン区切りの文字列にまとめます。"""
        if not values:
            return None
        non_null_values = [str(value) for value in values if value is not None]
        if not non_null_values:
            return None
        return "; ".join(non_null_values)

    @staticmethod
    def _set_atlas_filter_expression(atlas, expression):
        """地図帳の抽出式を設定し、QGISから失敗が返されたら例外にします。"""
        result = atlas.setFilterExpression(expression)
        if isinstance(result, tuple):
            succeeded, error = result
        else:
            succeeded, error = result, ""
        if not succeeded:
            # QGIS clears the expression before reporting the empty expression as invalid.
            if not expression and atlas.filterExpression() == expression:
                return
            raise RuntimeError(
                "地図帳の抽出条件を設定できませんでした。{}".format(
                    "\n" + error if error else ""
                )
            )

    @staticmethod
    def _unique_field_name(name, used_names):
        """項目名の重複を避け、まだ使われていない名前を返します。"""
        candidate = name
        suffix = 2
        while candidate.casefold() in used_names:
            candidate = "{} ({})".format(name, suffix)
            suffix += 1
        used_names.add(candidate.casefold())
        return candidate

    def _show_error(self, message, error=None):
        """エラー内容をQGISのダイアログで利用者に表示します。"""
        details = "{}\n\n{}".format(message, error) if error else message
        QMessageBox.critical(self.iface.mainWindow(), "KK_intersect_to_print", details)


class _PointCaptureTool(QgsMapToolEmitPoint):
    """地図上のクリック位置を受け取り、プラグイン本体へ渡すツールです。"""

    def __init__(self, plugin):
        # 親クラスに地図キャンバスを渡すと、QGISの地図操作ツールとして動作します。
        super().__init__(plugin.canvas)
        self.plugin = plugin

    def canvasReleaseEvent(self, event):
        # 画面上のマウス位置を地図座標へ変換してから、ポイント作成処理を呼びます。
        self.plugin.capture_point(self.toMapCoordinates(event.pos()))
